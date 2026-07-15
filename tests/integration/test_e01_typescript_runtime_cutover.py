from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
for relative in (
    "packages/core",
    "packages/runtime",
    "packages/workers",
    "packages/workspace",
    "packages/skills",
    "packages/integrations",
    "packages/orchestration",
):
    path = str(REPO_ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

from zyra_runtime.workers import WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402


def _runtime(tmp_path: Path) -> CodeWorkerRuntime:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )


def _request(run_id: str, **constraints: object) -> WorkerRequest:
    return WorkerRequest(
        run_id=run_id,
        task_id="e01-task",
        worker_name="CodeWorkerRuntime",
        constraints={"query_turns": [], "permission_mode": "sealed", **constraints},
    )


def _checkpoint(run: object) -> dict[str, object]:
    worker_result = getattr(run, "worker_result")
    path = Path(worker_result.metadata["runtime_state_checkpoint_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _typescript_snapshot(run: object) -> dict[str, object]:
    payload = _checkpoint(run)
    session_snapshot = payload["session_snapshot"]
    assert isinstance(session_snapshot, dict)
    snapshot = session_snapshot["typescript_runtime_snapshot"]
    assert isinstance(snapshot, dict)
    return snapshot


@pytest.fixture
def completed_run(tmp_path: Path) -> object:
    return _runtime(tmp_path).run(
        _request("e01-completed-fixture", session_id="e01-fixture-session")
    )


def test_default_code_worker_reaches_typescript_owner(tmp_path: Path) -> None:
    run = _runtime(tmp_path).run(_request("e01-default"))

    assert run.worker_result.ok is True
    assert run.worker_result.error is None
    assert run.worker_result.metadata["canonical_runtime_owner"] == "typescript"
    assert run.worker_result.metadata["python_policy_fallback"] == "false"
    assert run.worker_result.metadata["python_query_engine_fallback"] == "false"
    assert Path(run.worker_result.metadata["runtime_state_checkpoint_path"]).is_file()
    assert len(run.event_records) >= 2


def test_environment_disconnect_fails_without_python_fallback(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {"ZYRA_DISABLE_TYPESCRIPT_RUNTIME": "1"}):
        run = _runtime(tmp_path).run(_request("e01-environment-disabled"))

    assert run.worker_result.ok is False
    assert run.worker_result.error == "typescript_runtime_disabled"
    assert run.worker_result.metadata["canonical_runtime_owner"] == "typescript"
    assert run.worker_result.metadata["python_policy_fallback"] == "false"
    assert run.worker_result.metadata["python_query_engine_fallback"] == "false"


def test_configuration_disconnect_fails_before_process_launch(tmp_path: Path) -> None:
    run = _runtime(tmp_path).run(
        _request("e01-configuration-disabled", disable_typescript_runtime=True)
    )

    assert run.worker_result.ok is False
    assert run.worker_result.error == "productized_query_engine_runtime_disabled"
    assert run.worker_result.metadata["canonical_runtime_owner"] == "typescript"
    assert run.worker_result.metadata["python_policy_fallback"] == "false"


def test_checkpoint_is_reused_for_same_session(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = runtime.run(_request("e01-resume", session_id="e01-stable-session"))
    second = runtime.run(_request("e01-resume", session_id="e01-stable-session"))

    assert first.worker_result.ok is True
    assert second.worker_result.ok is True
    assert (
        first.worker_result.metadata["runtime_state_checkpoint_path"]
        == second.worker_result.metadata["runtime_state_checkpoint_path"]
    )
    assert second.worker_result.metadata["query_session_id"] == "e01-stable-session"


def test_tool_plan_is_normalized_and_executed_by_typescript(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "proof.txt").write_text("typescript custody proof", encoding="utf-8")
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )

    run = runtime.run(
        _request(
            "e01-tool-plan",
            tool_plan=[
                {
                    "tool_name": "file_read",
                    "arguments": {"path": "proof.txt"},
                }
            ],
        )
    )

    assert run.worker_result.ok is True
    assert run.worker_result.metadata["tool_steps"] == "1"
    assert run.worker_result.metadata["canonical_runtime_owner"] == "typescript"
    assert any(
        "typescript custody proof" in json.dumps(event, ensure_ascii=True)
        for event in run.worker_result.events
    )


def test_checkpoint_contains_e01_journal_and_runtime_identity(
    completed_run: object,
) -> None:
    snapshot = _typescript_snapshot(completed_run)

    assert snapshot["canonical_owner"] == "typescript"
    assert snapshot["runtime_id"] == "zyra-typescript-claude-runtime"
    assert snapshot["version"] == "zyra.typescript-query-session.v1"
    e01 = snapshot["e01Runtime"]
    assert isinstance(e01, dict)
    committed = e01["committed"]
    assert isinstance(committed, list)
    assert len(committed) >= 40
    assert e01["pending"] == []
    assert int(e01["revision"]) == len(committed)


def test_e01_commits_have_stable_identity_and_acknowledged_outbox(
    completed_run: object,
) -> None:
    e01 = _typescript_snapshot(completed_run)["e01Runtime"]
    assert isinstance(e01, dict)
    committed = e01["committed"]
    assert isinstance(committed, list)

    revisions: list[int] = []
    idempotency_keys: set[str] = set()
    for record in committed:
        assert isinstance(record, dict)
        assert record["phase"] == "ack"
        assert record["status"] == "acked"
        assert record["after"] == int(record["before"]) + 1
        assert record["outbox"]
        identity = record["id"]
        assert isinstance(identity, dict)
        key = str(identity["idempotencyKey"])
        assert key.startswith("sha256:")
        assert key not in idempotency_keys
        idempotency_keys.add(key)
        assert identity["runId"] == "e01-completed-fixture"
        assert identity["sessionId"] == "e01-fixture-session"
        revisions.append(int(record["after"]))

    assert revisions == list(range(1, len(committed) + 1))


def test_e01_bootstrap_covers_each_cutover_domain(completed_run: object) -> None:
    e01 = _typescript_snapshot(completed_run)["e01Runtime"]
    assert isinstance(e01, dict)
    committed = e01["committed"]
    assert isinstance(committed, list)
    domains = {str(record["domain"]) for record in committed if isinstance(record, dict)}

    assert {
        "query.transition",
        "query.turn",
        "input.normalize",
        "context.assemble",
        "tools.registry",
        "tools.execute",
        "compact.restore",
        "provider.request",
        "session.lifecycle",
        "protocol.recovery",
        "protocol.fence",
        "protocol.idempotency",
    }.issubset(domains)


def test_corrupt_host_checkpoint_is_not_used_as_runtime_state(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = runtime.run(
        _request("e01-corrupt-checkpoint", session_id="e01-corrupt-session")
    )
    checkpoint = Path(
        first.worker_result.metadata["runtime_state_checkpoint_path"]
    )
    checkpoint.write_text("{not-valid-json", encoding="utf-8")

    second = runtime.run(
        _request("e01-corrupt-checkpoint", session_id="e01-corrupt-session")
    )

    assert second.worker_result.ok is True
    repaired = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert repaired["canonical_owner"] == "typescript"
    assert repaired["session_id"] == "e01-corrupt-session"
    assert repaired["session_snapshot"]["typescript_runtime_snapshot"][
        "canonical_owner"
    ] == "typescript"


def test_invalid_tool_arguments_fail_before_side_effect(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )

    run = runtime.run(
        _request(
            "e01-invalid-tool",
            query_turns=[
                [
                    {
                        "tool_name": "file_write",
                        "arguments": {"content": "must not be written"},
                    }
                ]
            ],
        )
    )

    assert run.worker_result.ok is False
    assert run.worker_result.metadata["canonical_runtime_owner"] == "typescript"
    assert run.worker_result.metadata["python_policy_fallback"] == "false"
    assert list(workspace.iterdir()) == []
