from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from zyra_orchestration.deployment import code_worker_adapter
from zyra_orchestration.deployment.task_mutation_policy import (
    TaskMutationPolicy,
    TaskMutationPolicyGuard,
    TaskMutationPolicyViolation,
)
from zyra_runtime.sandbox_gateway import (
    BackendSession,
    CancellationToken,
    DockerCliSandboxConnector,
    GatewayCommandEnvelope,
    ProcessOutput,
    ProcessResult,
    ProcessTermination,
)


def _entry(content: bytes) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }


def _guard(tmp_path, *, root: str = "sources/widget") -> TaskMutationPolicyGuard:
    return TaskMutationPolicyGuard(
        TaskMutationPolicy.from_mapping(
            {
                "enabled": True,
                "protected_source_roots": [root],
                "required_pre_mutation_evidence": ["existing_test_baseline"],
                "protect_existing_test_files": True,
                "inherit_across_execution_lineage": True,
            }
        ),
        state_root=tmp_path / "policy-state",
    )


def test_mutation_guard_persists_baseline_and_protects_source_and_existing_tests(
    tmp_path,
) -> None:
    guard = _guard(tmp_path)

    with pytest.raises(TaskMutationPolicyViolation, match="protects source input"):
        guard.assert_mutation("sources/widget/lib/core.py", existed=True)
    with pytest.raises(TaskMutationPolicyViolation, match="requires a completed"):
        guard.assert_mutation("widget-copy/lib/core.py", existed=True)

    assert guard.observe_command(
        executable="python",
        argv=("-m", "compileall", "."),
        metadata={"progressive_verification_driving": True},
        termination="exited",
        return_code=0,
        command_id="compile-command",
    ) is False
    assert guard.observe_command(
        executable="python",
        argv=("-m", "unittest", "discover", "-s", "tests"),
        metadata={"progressive_verification_driving": "true"},
        termination="exited",
        return_code=1,
        command_id="baseline-command",
    ) is True

    guard.assert_mutation("widget-copy/lib/core.py", existed=True)
    with pytest.raises(TaskMutationPolicyViolation, match="existing test file"):
        guard.assert_mutation("widget-copy/tests/test_core.py", existed=True)
    guard.assert_mutation("widget-copy/tests/test_regression.py", existed=False)

    reloaded = _guard(tmp_path)
    assert reloaded.baseline_satisfied is True


@pytest.mark.parametrize(
    ("root", "source", "existing_test", "new_test"),
    (
        (
            "sources/python-lib",
            "sources/python-lib/lib.py",
            "repair/tests/test_lib.py",
            "repair/tests/test_regression.py",
        ),
        (
            "fixtures/web-client",
            "fixtures/web-client/src/client.ts",
            "repair/src/client.test.ts",
            "repair/src/retry.spec.ts",
        ),
    ),
)
def test_policy_delta_is_parameterized_across_paths_and_languages(
    tmp_path,
    root: str,
    source: str,
    existing_test: str,
    new_test: str,
) -> None:
    guard = _guard(tmp_path, root=root)
    before = {
        source: _entry(b"source-before"),
        existing_test: _entry(b"test-before"),
        "repair/lib/output.txt": _entry(b"output-before"),
    }
    after = {
        source: _entry(b"source-after"),
        existing_test: _entry(b"test-after"),
        "repair/lib/output.txt": _entry(b"output-after"),
        new_test: _entry(b"new-test"),
    }

    assert set(
        guard.prohibited_delta(
            before,
            after,
            baseline_satisfied_before=True,
        )
    ) == {source, existing_test}
    assert set(
        guard.prohibited_delta(
            before,
            after,
            baseline_satisfied_before=False,
        )
    ) == set(after)


def test_shell_transaction_restores_protected_bytes_and_keeps_allowed_delta(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "sources" / "widget" / "lib.py"
    existing_test = workspace / "repair" / "tests" / "test_lib.py"
    output = workspace / "repair" / "lib.py"
    for path, content in (
        (source, "source-before"),
        (existing_test, "test-before"),
        (output, "output-before"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    guard = _guard(tmp_path)
    assert guard.observe_command(
        executable="pytest",
        argv=("repair/tests",),
        metadata={"progressive_verification_driving": True},
        termination="exited",
        return_code=1,
        command_id="baseline-command",
    )
    snapshot = code_worker_adapter._capture_task_policy_transaction(
        workspace,
        guard,
        excluded_prefixes=(),
    )

    source.write_text("source-after", encoding="utf-8")
    existing_test.write_text("test-after", encoding="utf-8")
    output.write_text("output-after", encoding="utf-8")
    new_test = workspace / "repair" / "tests" / "test_regression.py"
    new_test.write_text("new-test", encoding="utf-8")

    prohibited, _restored = code_worker_adapter._restore_task_policy_transaction(
        snapshot,
        guard,
    )

    assert set(prohibited) == {
        "sources/widget/lib.py",
        "repair/tests/test_lib.py",
    }
    assert source.read_text(encoding="utf-8") == "source-before"
    assert existing_test.read_text(encoding="utf-8") == "test-before"
    assert output.read_text(encoding="utf-8") == "output-after"
    assert new_test.read_text(encoding="utf-8") == "new-test"


def test_benchmark_connector_reverts_prebaseline_shell_and_records_real_test(
    tmp_path,
) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / "task-workspace"
    sync_root = tmp_path / "sync"
    workspace.mkdir(parents=True)
    sync_root.mkdir()
    source = workspace / "sources" / "widget" / "lib.py"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    binding = {
        "container": "governed-worker",
        "container_ref_digest": "digest",
        "workdir": "/workspace",
        "docker_executable": "docker-test",
        "workspace_data_root": data_root,
        "sync_root": sync_root,
        "canonical_host_workspace": workspace,
    }
    mirror = code_worker_adapter._BenchmarkWorkspaceMirror(
        binding,
        workspace,
        synced_manifest=code_worker_adapter._workspace_manifest(workspace),
    )
    guard = _guard(tmp_path)
    connector = code_worker_adapter._BenchmarkDockerCliSandboxConnector(
        container="governed-worker",
        workdir="/workspace",
        docker_executable="docker-test",
        benchmark_mirror=mirror,
        mutation_policy_guard=guard,
    )
    session = BackendSession(
        session_id="session",
        backend_id=connector.backend_id,
        execution_root=workspace,
        generation=1,
        prepared_at=0.0,
    )

    def process(envelope: GatewayCommandEnvelope, *, return_code: int) -> ProcessResult:
        return ProcessResult(
            command_id=envelope.command_id,
            termination=ProcessTermination.EXITED,
            return_code=return_code,
            started_at=1.0,
            finished_at=2.0,
            output=ProcessOutput(),
            backend_id=connector.backend_id,
        )

    mutation = GatewayCommandEnvelope.build(
        session_id="session",
        run_id="run",
        task_id="task",
        worker_id="worker",
        executable="python",
        argv=("-c", "write output"),
        metadata={"progressive_delivery_driving_shell": True},
    )

    def mutate(*_args, **_kwargs):
        (workspace / "repair.py").write_text("too early", encoding="utf-8")
        return process(mutation, return_code=0)

    with (
        patch.object(
            code_worker_adapter,
            "_push_benchmark_workspace_delta",
            return_value={
                "written_count": 0,
                "deleted_count": 0,
                "written_path_digests": [],
                "deleted_path_digests": [],
            },
        ),
        patch.object(DockerCliSandboxConnector, "execute", side_effect=mutate),
    ):
        denied = connector.execute(session, mutation, CancellationToken())

    assert denied.error_code == "task_mutation_policy_denied"
    assert denied.return_code == 126
    assert not (workspace / "repair.py").exists()
    assert guard.baseline_satisfied is False

    baseline = GatewayCommandEnvelope.build(
        session_id="session",
        run_id="run",
        task_id="task",
        worker_id="worker",
        executable="python",
        argv=("-m", "unittest", "discover"),
        metadata={"progressive_verification_driving": True},
    )
    with (
        patch.object(
            code_worker_adapter,
            "_push_benchmark_workspace_delta",
            return_value={
                "written_count": 0,
                "deleted_count": 0,
                "written_path_digests": [],
                "deleted_path_digests": [],
            },
        ),
        patch.object(
            DockerCliSandboxConnector,
            "execute",
            return_value=process(baseline, return_code=1),
        ),
    ):
        observed = connector.execute(session, baseline, CancellationToken())

    assert observed.return_code == 1
    assert guard.baseline_satisfied is True


def test_benchmark_connector_restores_prebaseline_delta_when_backend_raises(
    tmp_path,
) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / "task-workspace"
    sync_root = tmp_path / "sync"
    workspace.mkdir(parents=True)
    sync_root.mkdir()
    protected = workspace / "sources" / "widget" / "lib.py"
    protected.parent.mkdir(parents=True)
    protected.write_text("original", encoding="utf-8")
    binding = {
        "container": "governed-worker",
        "container_ref_digest": "digest",
        "workdir": "/workspace",
        "docker_executable": "docker-test",
        "workspace_data_root": data_root,
        "sync_root": sync_root,
        "canonical_host_workspace": workspace,
    }
    mirror = code_worker_adapter._BenchmarkWorkspaceMirror(
        binding,
        workspace,
        synced_manifest=code_worker_adapter._workspace_manifest(workspace),
    )
    guard = _guard(tmp_path)
    connector = code_worker_adapter._BenchmarkDockerCliSandboxConnector(
        container="governed-worker",
        workdir="/workspace",
        docker_executable="docker-test",
        benchmark_mirror=mirror,
        mutation_policy_guard=guard,
    )
    session = BackendSession(
        session_id="variant-session",
        backend_id=connector.backend_id,
        execution_root=workspace,
        generation=1,
        prepared_at=0.0,
    )
    command = GatewayCommandEnvelope.build(
        session_id="variant-session",
        run_id="variant-run",
        task_id="variant-task",
        worker_id="variant-worker",
        executable="node",
        argv=("-e", "mutate then crash"),
        metadata={"progressive_delivery_driving_shell": True},
    )

    def crash_after_mutation(*_args, **_kwargs):
        protected.write_text("corrupted", encoding="utf-8")
        (workspace / "premature-output.txt").write_text(
            "not committed",
            encoding="utf-8",
        )
        raise RuntimeError("backend transport failed after side effects")

    with (
        patch.object(
            code_worker_adapter,
            "_push_benchmark_workspace_delta",
            return_value={
                "written_count": 0,
                "deleted_count": 0,
                "written_path_digests": [],
                "deleted_path_digests": [],
            },
        ),
        patch.object(
            DockerCliSandboxConnector,
            "execute",
            side_effect=crash_after_mutation,
        ),
        pytest.raises(RuntimeError, match="backend transport failed"),
    ):
        connector.execute(session, command, CancellationToken())

    assert protected.read_text(encoding="utf-8") == "original"
    assert not (workspace / "premature-output.txt").exists()
    assert guard.baseline_satisfied is False
