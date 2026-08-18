from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
from zyra_runtime.executor import ToolExecutor  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402
from zyra_workers.typescript_claude_runtime import (  # noqa: E402
    TypeScriptClaudeQueryEngine,
    TypeScriptRuntimeError,
    load_task_handoff_projection,
)


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


def _e01_journal(run: object) -> dict[str, object]:
    e01 = _typescript_snapshot(run)["e01Runtime"]
    assert isinstance(e01, dict)
    journal = e01.get("journal", e01)
    assert isinstance(journal, dict)
    return journal


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


def test_runtime_state_capsule_does_not_duplicate_complete_snapshot(
    tmp_path: Path,
) -> None:
    run = _runtime(tmp_path).run(
        _request("e01-compact-resume-capsule", session_id="e01-compact-capsule")
    )

    session_snapshot = _checkpoint(run)["session_snapshot"]
    assert isinstance(session_snapshot, dict)
    runtime_state = session_snapshot["runtime_state"]
    assert isinstance(runtime_state, dict)
    capsule = runtime_state["session_snapshot"]
    assert isinstance(capsule, dict)
    assert capsule["schema"] == "zyra.typescript-runtime.resume-capsule/v1"
    assert capsule["session_id"] == "e01-compact-capsule"
    assert capsule["resume_token"] == session_snapshot["resume_token"]
    assert capsule["host_checkpoint_revision"] == session_snapshot[
        "host_checkpoint_revision"
    ]
    assert "typescript_runtime_snapshot" not in capsule
    assert "transcript" not in capsule
    assert "messages" not in capsule


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
    assert run.worker_result.error == "typescript_runtime_disabled"
    assert run.worker_result.metadata["canonical_runtime_owner"] == "typescript"
    assert run.worker_result.metadata["python_policy_fallback"] == "false"
    assert run.worker_result.metadata["python_query_engine_fallback"] == "false"


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
    journal = _e01_journal(completed_run)
    committed = journal["committed"]
    assert isinstance(committed, list)
    assert len(committed) >= 10
    assert journal["pending"] == []
    assert int(journal["revision"]) == len(committed)


def test_e01_commits_have_stable_identity_and_acknowledged_outbox(
    completed_run: object,
) -> None:
    journal = _e01_journal(completed_run)
    committed = journal["committed"]
    assert isinstance(committed, list)

    revisions: list[int] = []
    idempotency_keys: set[str] = set()
    for record in committed:
        assert isinstance(record, dict)
        assert record["phase"] == "ack"
        assert record["status"] == "acked"
        assert record["revisionAfter"] == int(record["revisionBefore"]) + 1
        assert record["outboxIds"]
        identity = record["identity"]
        assert isinstance(identity, dict)
        key = str(identity["idempotencyKey"])
        assert key.startswith("sha256:")
        assert key not in idempotency_keys
        idempotency_keys.add(key)
        assert identity["runId"] == "e01-completed-fixture"
        assert identity["sessionId"] == "e01-fixture-session"
        revisions.append(int(record["revisionAfter"]))

    assert revisions == list(range(1, len(committed) + 1))


def test_e01_bootstrap_covers_each_cutover_domain(completed_run: object) -> None:
    journal = _e01_journal(completed_run)
    committed = journal["committed"]
    assert isinstance(committed, list)
    domains = {str(record["domain"]) for record in committed if isinstance(record, dict)}

    assert {"session", "context", "provider"}.issubset(domains)


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


def test_default_path_persists_incremental_typescript_checkpoint(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    run = runtime.run(
        _request(
            "e01-incremental-checkpoint",
            session_id="e01-incremental-session",
        )
    )

    assert run.worker_result.ok is True
    checkpoint_dir = tmp_path / "artifacts" / ".runtime-checkpoints"
    checkpoint_files = [
        path
        for path in checkpoint_dir.glob("typescript-e01-*.json")
        if not path.name.endswith(".handoff.json")
    ]
    assert len(checkpoint_files) == 1
    checkpoint = json.loads(checkpoint_files[0].read_text(encoding="utf-8"))
    assert checkpoint["session_id"] == "e01-incremental-session"
    assert checkpoint["canonical_owner"] == "typescript"
    assert int(checkpoint["checkpointEventSequence"]) > 0
    assert checkpoint["checkpointPhase"]


def test_terminal_result_is_durable_before_host_ack(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    original_write_frame = TypeScriptClaudeQueryEngine._write_frame
    observed: dict[str, object] = {}

    def observe_terminal_ack(*args: object, **kwargs: object) -> object:
        if kwargs.get("kind") == "run.result.ack":
            checkpoint_files = [
                path
                for path in (
                    tmp_path / "artifacts" / ".runtime-checkpoints"
                ).glob("typescript-e01-*.json")
                if not path.name.endswith(".handoff.json")
            ]
            assert len(checkpoint_files) == 1
            checkpoint = json.loads(
                checkpoint_files[0].read_text(encoding="utf-8")
            )
            receipts = checkpoint["terminal_result_receipts"]
            assert isinstance(receipts, dict)
            receipt = next(iter(receipts.values()))
            assert receipt["state"] == "committed_before_ack"
            assert receipt["terminal_id"] == kwargs["payload"]["terminal_id"]
            observed["terminal_id"] = receipt["terminal_id"]
        return original_write_frame(*args, **kwargs)

    with mock.patch.object(
        TypeScriptClaudeQueryEngine,
        "_write_frame",
        autospec=True,
        side_effect=observe_terminal_ack,
    ):
        run = runtime.run(
            _request(
                "e04-terminal-durable-before-ack",
                session_id="e04-terminal-durable-before-ack-session",
            )
        )

    assert run.worker_result.ok is True
    assert observed["terminal_id"]
    assert run.worker_result.metadata["terminal_commit_protocol"] == (
        "close-result-ack-closed"
    )


def test_lost_terminal_ack_resumes_without_tool_reexecution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "terminal.txt").write_text("terminal receipt", encoding="utf-8")
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )
    original_execute = ToolExecutor.execute
    original_write_frame = TypeScriptClaudeQueryEngine._write_frame
    executions = 0
    dropped = False

    def counting_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        nonlocal executions
        executions += 1
        return original_execute(self, call, **kwargs)

    def drop_first_terminal_ack(*args: object, **kwargs: object) -> object:
        nonlocal dropped
        if kwargs.get("kind") == "run.result.ack" and not dropped:
            dropped = True
            raise OSError("simulated lost terminal acknowledgement")
        return original_write_frame(*args, **kwargs)

    request = _request(
        "e04-lost-terminal-ack",
        session_id="e04-lost-terminal-ack-session",
        query_turns=[
            [{"tool_name": "file_read", "arguments": {"path": "terminal.txt"}}]
        ],
    )
    with (
        mock.patch.object(ToolExecutor, "execute", counting_execute),
        mock.patch.object(
            TypeScriptClaudeQueryEngine,
            "_write_frame",
            autospec=True,
            side_effect=drop_first_terminal_ack,
        ),
    ):
        first = runtime.run(request)

    with (
        mock.patch.object(ToolExecutor, "execute", counting_execute),
        mock.patch(
            "zyra_workers.typescript_claude_runtime.subprocess.Popen",
            wraps=subprocess.Popen,
        ) as runtime_launch,
    ):
        resumed = runtime.run(request)

    assert first.worker_result.ok is False
    assert dropped is True
    assert resumed.worker_result.ok is True
    assert executions == 1
    assert runtime_launch.call_count == 0
    checkpoint = _checkpoint(resumed)
    session_snapshot = checkpoint["session_snapshot"]
    assert isinstance(session_snapshot, dict)
    receipts = session_snapshot["terminal_result_receipts"]
    assert isinstance(receipts, dict)
    assert len(receipts) == 1


def test_duplicate_terminal_delivery_is_acknowledged_idempotently(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    original_read_frame = TypeScriptClaudeQueryEngine._read_frame
    original_write_frame = TypeScriptClaudeQueryEngine._write_frame
    duplicate: dict[str, object] | None = None
    duplicated = False
    injected_sequence_offset = 0
    terminal_ack_attempts = 0

    def duplicate_terminal_result(
        *args: object, **kwargs: object
    ) -> dict[str, object]:
        nonlocal duplicate, duplicated, injected_sequence_offset
        if duplicate is not None:
            frame = dict(duplicate)
            frame["sequence"] = kwargs["expected_sequence"]
            duplicate = None
            injected_sequence_offset += 1
            return frame
        adjusted_kwargs = dict(kwargs)
        adjusted_kwargs["expected_sequence"] = (
            int(kwargs["expected_sequence"]) - injected_sequence_offset
        )
        frame = original_read_frame(*args, **adjusted_kwargs)
        if frame.get("kind") == "run.result" and not duplicated:
            duplicate = dict(frame)
            duplicated = True
        return frame

    def count_terminal_ack(*args: object, **kwargs: object) -> object:
        nonlocal terminal_ack_attempts
        if kwargs.get("kind") == "run.result.ack":
            terminal_ack_attempts += 1
            if terminal_ack_attempts == 2:
                # The real runtime consumes one ACK and emits run.closed. The
                # second delivery is injected at the host boundary, so its
                # idempotent ACK has no additional peer request to settle.
                return None
        return original_write_frame(*args, **kwargs)

    with (
        mock.patch.object(
            TypeScriptClaudeQueryEngine,
            "_read_frame",
            autospec=True,
            side_effect=duplicate_terminal_result,
        ),
        mock.patch.object(
            TypeScriptClaudeQueryEngine,
            "_write_frame",
            autospec=True,
            side_effect=count_terminal_ack,
        ),
    ):
        run = runtime.run(
            _request(
                "e04-duplicate-terminal-delivery",
                session_id="e04-duplicate-terminal-delivery-session",
            )
        )

    assert run.worker_result.ok is True
    assert duplicated is True
    assert terminal_ack_attempts == 2
    session_snapshot = _checkpoint(run)["session_snapshot"]
    assert isinstance(session_snapshot, dict)
    receipts = session_snapshot["terminal_result_receipts"]
    assert isinstance(receipts, dict)
    assert len(receipts) == 1


def test_default_path_runs_read_only_batch_concurrently(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "one.txt").write_text("one", encoding="utf-8")
    (workspace / "two.txt").write_text("two", encoding="utf-8")
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )
    intervals: dict[str, list[float]] = {}
    original_execute = ToolExecutor.execute

    def delayed_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        interval = intervals.setdefault(str(getattr(call, "tool_call_id")), [])
        interval.append(time.perf_counter())
        time.sleep(0.2)
        result = original_execute(self, call, **kwargs)
        interval.append(time.perf_counter())
        return result

    with mock.patch.object(ToolExecutor, "execute", delayed_execute):
        run = runtime.run(
            _request(
                "e01-concurrent-read-only",
                session_id="e01-concurrent-session",
                query_turns=[
                    [
                        {"tool_name": "file_read", "arguments": {"path": "one.txt"}},
                        {"tool_name": "file_read", "arguments": {"path": "two.txt"}},
                    ]
                ],
            )
        )

    assert run.worker_result.ok is True
    assert len(intervals) == 2
    assert all(len(interval) == 2 for interval in intervals.values())
    starts = [interval[0] for interval in intervals.values()]
    ends = [interval[1] for interval in intervals.values()]
    assert max(starts) < min(ends)
    session_snapshot = _checkpoint(run)["session_snapshot"]
    assert isinstance(session_snapshot, dict)
    evidence = session_snapshot["tool_batch_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["execution_mode"] == "concurrent_read_only"
    assert evidence["request_count"] == 2


def test_lost_tool_batch_ack_resumes_without_reexecution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "proof.txt").write_text("durable receipt", encoding="utf-8")
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )
    original_execute = ToolExecutor.execute
    original_write_frame = TypeScriptClaudeQueryEngine._write_frame
    executions = 0
    dropped = False

    def counting_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        nonlocal executions
        executions += 1
        return original_execute(self, call, **kwargs)

    def drop_first_batch_result(*args: object, **kwargs: object) -> object:
        nonlocal dropped
        if kwargs.get("kind") == "tool.batch.result" and not dropped:
            dropped = True
            raise OSError("simulated lost tool batch acknowledgement")
        return original_write_frame(*args, **kwargs)

    request = _request(
        "e01-lost-batch-ack",
        session_id="e01-lost-batch-ack-session",
        query_turns=[
            [{"tool_name": "file_read", "arguments": {"path": "proof.txt"}}]
        ],
    )
    with (
        mock.patch.object(ToolExecutor, "execute", counting_execute),
        mock.patch.object(
            TypeScriptClaudeQueryEngine,
            "_write_frame",
            autospec=True,
            side_effect=drop_first_batch_result,
        ),
    ):
        first = runtime.run(request)
    assert first.worker_result.ok is False
    assert dropped is True
    assert first.worker_result.metadata["query_turns"] == "1"
    assert first.worker_result.metadata["tool_steps"] == "1"
    failed_checkpoint = _checkpoint(first)
    failed_snapshot = failed_checkpoint["session_snapshot"]
    assert isinstance(failed_snapshot, dict)
    assert failed_snapshot["stats"]["turn_count"] == 1
    trace_artifact = next(
        artifact
        for artifact in first.worker_result.artifacts
        if artifact.title.startswith("CodeWorker E01 trace")
    )
    trace_text = runtime.execution_context.artifact_store.resolve_path(
        trace_artifact
    ).read_text(encoding="utf-8")
    assert "- turns: `1`" in trace_text
    assert "- tool_calls: `1`" in trace_text

    with mock.patch.object(ToolExecutor, "execute", counting_execute):
        resumed = runtime.run(request)

    assert resumed.worker_result.ok is True, json.dumps(
        resumed.worker_result.events[-1], ensure_ascii=False
    )
    assert executions == 1
    session_snapshot = _checkpoint(resumed)["session_snapshot"]
    assert isinstance(session_snapshot, dict)
    receipts = session_snapshot["tool_effect_receipts"]
    assert isinstance(receipts, dict)
    assert len(receipts) == 1


def test_dispatched_non_idempotent_tool_is_fenced_after_restart(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )
    original_execute = ToolExecutor.execute
    executions = 0

    def counting_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        nonlocal executions
        executions += 1
        return original_execute(self, call, **kwargs)

    request = _request(
        "e01-dispatch-outcome-unknown",
        session_id="e01-dispatch-outcome-unknown-session",
        permission_mode="acceptEdits",
        query_turns=[[
            {
                "tool_name": "file_write",
                "arguments": {"path": "effect.txt", "content": "must execute once"},
            }
        ]],
    )
    with mock.patch.object(ToolExecutor, "execute", counting_execute):
        failed = runtime.run(
            _request(
                "e01-dispatch-outcome-unknown",
                session_id="e01-dispatch-outcome-unknown-session",
                permission_mode="acceptEdits",
                query_turns=[[
                    {
                        "tool_name": "file_write",
                        "arguments": {
                            "path": "effect.txt",
                            "content": "must execute once",
                        },
                    }
                ]],
                typescript_fault_injection="tool_dispatched_before_execution",
            )
        )
        recovered = runtime.run(request)

    assert failed.worker_result.ok is False
    failed_snapshot = _checkpoint(failed)["session_snapshot"]
    assert isinstance(failed_snapshot, dict)
    failed_receipts = failed_snapshot["tool_effect_receipts"]
    assert isinstance(failed_receipts, dict) and len(failed_receipts) == 1, (
        failed.worker_result.error,
        failed.worker_result.events[-1],
        failed_receipts,
    )
    assert executions == 0
    assert not (workspace / "effect.txt").exists()
    recovered_snapshot = _checkpoint(recovered)["session_snapshot"]
    assert isinstance(recovered_snapshot, dict)
    receipts = recovered_snapshot["tool_effect_receipts"]
    assert isinstance(receipts, dict) and len(receipts) == 1
    receipt = next(iter(receipts.values()))
    assert receipt["transaction_state"] == "dispatched"
    assert receipt["recovery_attempts"] == 1
    assert receipt["result"] is None
    settlement = recovered_snapshot["typescript_runtime_snapshot"][
        "typescriptCapabilities"
    ]["settlement"]
    assert settlement["calls"][0]["transactionState"] == "outcome_unknown"
    assert settlement["calls"][0]["dispatchCredential"]


def test_pre_dispatch_intent_recovers_once_with_the_same_idempotency_key(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )
    original_execute = ToolExecutor.execute
    executions = 0

    def counting_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        nonlocal executions
        executions += 1
        return original_execute(self, call, **kwargs)

    common = {
        "session_id": "e01-pre-dispatch-intent-session",
        "permission_mode": "acceptEdits",
        "query_turns": [[{
            "tool_name": "file_write",
            "arguments": {"path": "intent.txt", "content": "one execution"},
        }]],
    }
    with mock.patch.object(ToolExecutor, "execute", counting_execute):
        failed = runtime.run(
            _request(
                "e01-pre-dispatch-intent",
                **common,
                typescript_fault_injection="tool_intent_before_dispatch",
            )
        )
        recovered = runtime.run(_request("e01-pre-dispatch-intent", **common))

    assert failed.worker_result.ok is False
    assert recovered.worker_result.ok is True
    assert executions == 1
    assert (workspace / "intent.txt").read_text(encoding="utf-8") == "one execution"
    snapshot = _checkpoint(recovered)["session_snapshot"]
    assert isinstance(snapshot, dict)
    receipts = snapshot["tool_effect_receipts"]
    assert isinstance(receipts, dict) and len(receipts) == 1
    receipt = next(iter(receipts.values()))
    assert receipt["transaction_state"] == "completed"
    assert receipt["recovery_attempts"] == 1
    assert receipt["effect_key"]


@pytest.mark.parametrize(
    ("fault_point", "minimum_launches", "minimum_epochs"),
    [
        ("checkpoint_request_before_ack", 2, 2),
        ("final_checkpoint_ack_before_terminal", 2, 2),
        ("terminal_result_ack_lost", 1, 1),
        ("python_host_terminal_disconnect", 2, 2),
        ("typescript_process_terminal_disconnect", 2, 2),
    ],
)
def test_real_process_terminal_fault_matrix_is_exactly_once(
    tmp_path: Path,
    fault_point: str,
    minimum_launches: int,
    minimum_epochs: int,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "terminal.txt").write_text("terminal effect", encoding="utf-8")
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
    )
    run_id = f"e04-real-fault-{fault_point}"
    session_id = f"{run_id}-session"
    turns = [[{"tool_name": "file_read", "arguments": {"path": "terminal.txt"}}]]
    original_execute = ToolExecutor.execute
    executions = 0

    def counting_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        nonlocal executions
        executions += 1
        return original_execute(self, call, **kwargs)

    with (
        mock.patch.object(ToolExecutor, "execute", counting_execute),
        mock.patch(
            "zyra_workers.typescript_claude_runtime.subprocess.Popen",
            wraps=subprocess.Popen,
        ) as runtime_launch,
    ):
        failed = runtime.run(
            _request(
                run_id,
                session_id=session_id,
                query_turns=turns,
                typescript_fault_injection=fault_point,
            )
        )
        recovered = runtime.run(
            _request(run_id, session_id=session_id, query_turns=turns)
        )

    assert failed.worker_result.ok is False
    assert recovered.worker_result.ok is True
    assert executions == 1
    assert runtime_launch.call_count >= minimum_launches
    snapshot = _checkpoint(recovered)["session_snapshot"]
    assert isinstance(snapshot, dict)
    faults = [
        item
        for item in snapshot["runtime_fault_receipts"]
        if item["point"] == fault_point
    ]
    assert len(faults) == 1
    assert faults[0]["real_process_kill"] is True
    assert isinstance(faults[0]["process_pid"], int)
    assert isinstance(faults[0]["process_exit_code"], int)
    trace = snapshot["protocol_frame_trace"]
    assert isinstance(trace, list) and trace
    assert [item["trace_index"] for item in trace] == list(range(1, len(trace) + 1))
    assert {item["direction"] for item in trace} == {
        "python-to-typescript",
        "typescript-to-python",
    }
    epochs = {int(item["runtime_process_epoch"]) for item in trace}
    assert len(epochs) >= minimum_epochs
    assert len(snapshot["terminal_result_receipts"]) == 1


def test_host_checkpoint_compare_and_swap_rejects_stale_writer(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    session_id = "e04-host-checkpoint-cas"
    current = TypeScriptClaudeQueryEngine(runtime.execution_context)
    stale = TypeScriptClaudeQueryEngine(runtime.execution_context)
    assert current._load_incremental_checkpoint(session_id) == {}
    assert stale._load_incremental_checkpoint(session_id) == {}
    committed = current._persist_incremental_checkpoint(session_id, {"value": 1})
    assert committed["host_checkpoint_revision"] == 1
    with pytest.raises(TypeScriptRuntimeError) as captured:
        stale._persist_incremental_checkpoint(session_id, {"value": 2})
    assert captured.value.code == "typescript_runtime_checkpoint_stale_writer"


def test_host_checkpoint_same_writer_skips_reparsing_unchanged_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    engine = TypeScriptClaudeQueryEngine(runtime.execution_context)
    session_id = "e04-host-checkpoint-fast-cas"
    first = engine._persist_incremental_checkpoint(session_id, {"value": 1})
    checkpoint_path = engine._checkpoint_path(session_id)
    real_read_text = Path.read_text

    def reject_checkpoint_reparse(path: Path, *args: object, **kwargs: object) -> str:
        if path == checkpoint_path:
            raise AssertionError("unchanged checkpoint should not be reparsed")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_checkpoint_reparse)
    second = engine._persist_incremental_checkpoint(session_id, {"value": 2})

    assert first["host_checkpoint_revision"] == 1
    assert second["host_checkpoint_parent_revision"] == 1
    assert second["host_checkpoint_revision"] == 2


def test_host_checkpoint_writes_bounded_cross_session_task_handoff(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    engine = TypeScriptClaudeQueryEngine(runtime.execution_context)
    session_id = "spent-permission-session"
    committed = engine._persist_incremental_checkpoint(
        session_id,
        {
            "task_id": "e01-task",
            "run_id": "run-handoff",
            "phase": "running",
            "turn_count": 19,
            "tool_call_count": 11,
            "compaction_count": 1,
            "progressiveExecution": {
                "providerRounds": 19,
                "workspaceMutationCount": 4,
                "verificationCount": 0,
                "unresolvedVerificationScopes": ["shell:afctl:test:integration"],
                "unresolvedVerificationFailures": [
                    {
                        "scope": "shell:afctl:test:integration",
                        "failedChecks": ["security", "state", "cross-language"],
                        "failedCount": 3,
                        "failureKind": "reported_checks",
                        "attemptCount": 2,
                        "lastObservedWorkspaceMutationCount": 4,
                    }
                ],
                "preDeliveryObservationCount": 13,
                "consecutivePreDeliveryObservations": 7,
                "actionNudgeCount": 3,
                "lastActionNudgeObservationCount": 7,
                "lastActionNudgeProviderRound": 18,
                "verificationDiagnosticVersion": 1,
                "repairContextId": "spent-permission-session",
            },
            "modelIteration": {
                "rounds": [
                    {
                        "roundIndex": 18,
                        "finalText": "Public tests now pass; next start the full stack.",
                    }
                ]
            },
            "messages": [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "ok": True,
                            "summary": "Sandbox command completed",
                            "output": {
                                "stdout": (
                                    "138 passed; api_key=should-never-cross-session; "
                                    "postgresql://worker:database-password@db/task"
                                )
                            },
                        }
                    ),
                }
            ],
        },
    )

    sidecar = engine._handoff_path(session_id)
    assert sidecar.is_file()
    assert sidecar.stat().st_size < 96_000
    encoded = sidecar.read_text(encoding="utf-8")
    assert "should-never-cross-session" not in encoded
    assert "database-password" not in encoded
    assert "[REDACTED]" in encoded
    handoff = load_task_handoff_projection(
        runtime.execution_context.artifact_store.root,
        task_id="e01-task",
        run_id="run-handoff",
        current_session_id="fresh-permission-session",
    )
    assert handoff is not None
    assert handoff["source_session_id"] == session_id
    assert handoff["source_checkpoint_commit_id"] == committed[
        "host_checkpoint_commit_id"
    ]
    assert handoff["progress"]["workspaceMutationCount"] == 4
    assert handoff["progress"]["unresolvedVerificationScopes"] == [
        "shell:afctl:test:integration"
    ]
    assert handoff["progress"]["actionNudgeCount"] == 3
    assert handoff["progress"]["lastActionNudgeProviderRound"] == 18
    assert handoff["progress"]["repairContextId"] == "spent-permission-session"
    assert handoff["inspection_continuity"] == {}
    assert handoff["execution_continuity"] == {
        "requiredDeliveryMissing": False,
        "providerRounds": 19,
        "workspaceMutationCount": 4,
        "verificationCount": 0,
        "verificationDiagnosticVersion": 1,
        "unresolvedVerificationScopes": ["shell:afctl:test:integration"],
        "unresolvedVerificationFailures": [
            {
                "scope": "shell:afctl:test:integration",
                "failedChecks": ["security", "state", "cross-language"],
                "failedCount": 3,
                "failureKind": "reported_checks",
                "attemptCount": 2,
                "lastObservedWorkspaceMutationCount": 4,
            }
        ],
        "repairContextId": "spent-permission-session",
    }
    assert handoff["recent_reasoning"][-1]["text"].endswith("full stack.")
    assert handoff["authority_transfer"] is False
    assert (
        load_task_handoff_projection(
            runtime.execution_context.artifact_store.root,
            task_id="e01-task",
            run_id="run-handoff",
            current_session_id=session_id,
        )
        is None
    )


def test_cross_session_handoff_keeps_rich_progress_when_latest_segment_is_sparse(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    checkpoint_root = (
        Path(runtime.execution_context.artifact_store.root) / ".runtime-checkpoints"
    )
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    rich_path = checkpoint_root / "typescript-e01-rich.json.handoff.json"
    sparse_path = checkpoint_root / "typescript-e01-sparse.json.handoff.json"
    common = {
        "schema": "zyra.typescript-runtime-handoff/v1",
        "task_id": "e01-task",
        "run_id": "run-handoff-chain",
        "authority_transfer": False,
        "claims_require_revalidation": True,
    }
    rich_path.write_text(
        json.dumps(
            {
                **common,
                "source_session_id": "rich-session",
                "source_checkpoint_revision": 220,
                "source_checkpoint_commit_id": "rich-commit",
                "phase": "tool_call_completed",
                "counters": {
                    "turn_count": 18,
                    "tool_call_count": 16,
                    "compaction_count": 1,
                },
                "progress": {
                    "providerRounds": 18,
                    "workspaceMutationCount": 4,
                    "verificationCount": 8,
                    "requiredDeliveryMissing": False,
                    "actionNudgeCount": 9,
                    "postDeliveryActionNudgeCount": 3,
                    "noDeliveryObservationCount": 24,
                    "consecutiveNoDeliveryObservations": 24,
                    "lastActionNudgeNoDeliveryObservationCount": 24,
                },
                "latest_compact_summary": "Public tests pass; start the full stack.",
                "recent_reasoning": [
                    {"round_index": 17, "text": "Continue with dynamic events."}
                ],
                "recent_tool_observations": [
                    {"ok": True, "summary": "138 tests passed", "excerpt": ""}
                ],
            }
        ),
        encoding="utf-8",
    )
    sparse_path.write_text(
        json.dumps(
            {
                **common,
                "source_session_id": "newest-sparse-session",
                "source_checkpoint_revision": 42,
                "source_checkpoint_commit_id": "sparse-commit",
                "phase": "tool_call_started",
                "counters": {
                    "turn_count": 2,
                    "tool_call_count": 2,
                    "compaction_count": 0,
                },
                "progress": {
                    "providerRounds": 2,
                    "workspaceMutationCount": 0,
                    "verificationCount": 0,
                    "unresolvedVerificationScopes": ["shell:afctl:test:integration"],
                    "unresolvedVerificationFailures": [
                        {
                            "scope": "shell:afctl:test:integration",
                            "failedChecks": ["security"],
                            "failedCount": 1,
                            "failureKind": "reported_checks",
                            "attemptCount": 1,
                            "lastObservedWorkspaceMutationCount": 3,
                        }
                    ],
                    "requiredDeliveryMissing": True,
                    "actionNudgeCount": 1,
                },
                "latest_compact_summary": "",
                "recent_reasoning": [
                    {"round_index": 1, "text": "A recovery probe was interrupted."}
                ],
                "recent_tool_observations": [],
            }
        ),
        encoding="utf-8",
    )
    os.utime(rich_path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(sparse_path, ns=(2_000_000_000, 2_000_000_000))

    handoff = load_task_handoff_projection(
        runtime.execution_context.artifact_store.root,
        task_id="e01-task",
        run_id="run-handoff-chain",
        current_session_id="fresh-session",
    )

    assert handoff is not None
    assert handoff["source_session_id"] == "newest-sparse-session"
    assert handoff["source_checkpoint_commit_id"] == "sparse-commit"
    assert handoff["continuity_segments_merged"] == 2
    assert handoff["counters"] == {
        "turn_count": 18,
        "tool_call_count": 16,
        "compaction_count": 1,
    }
    assert handoff["progress"]["workspaceMutationCount"] == 4
    assert handoff["progress"]["verificationCount"] == 0
    assert handoff["progress"]["unresolvedVerificationScopes"] == [
        "shell:afctl:test:integration"
    ]
    assert handoff["inspection_continuity"]["requiredDeliveryMissing"] is True
    assert handoff["inspection_continuity"]["actionNudgeCount"] == 1
    assert handoff["execution_continuity"] == {
        "requiredDeliveryMissing": False,
        "providerRounds": 18,
        "workspaceMutationCount": 4,
        "verificationCount": 0,
        "unresolvedVerificationScopes": ["shell:afctl:test:integration"],
        "unresolvedVerificationFailures": [
            {
                "scope": "shell:afctl:test:integration",
                "failedChecks": ["security"],
                "failedCount": 1,
                "failureKind": "reported_checks",
                "attemptCount": 1,
                "lastObservedWorkspaceMutationCount": 3,
            }
        ],
        "noDeliveryObservationCount": 24,
        "consecutiveNoDeliveryObservations": 24,
        "postDeliveryActionNudgeCount": 3,
        "lastActionNudgeNoDeliveryObservationCount": 24,
    }
    assert handoff["latest_compact_summary"].startswith("Public tests pass")
    assert [item["text"] for item in handoff["recent_reasoning"]] == [
        "Continue with dynamic events.",
        "A recovery probe was interrupted.",
    ]
    assert handoff["recent_tool_observations"][0]["summary"] == "138 tests passed"
    assert handoff["authority_transfer"] is False


def test_cross_session_handoff_recovers_safe_progress_from_legacy_sidecar(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    checkpoint_root = (
        Path(runtime.execution_context.artifact_store.root) / ".runtime-checkpoints"
    )
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_root / "typescript-e01-legacy.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "task_id": "legacy-task",
                "run_id": "legacy-run",
                "session_id": "legacy-session",
                "progressiveExecution": {
                    "providerRounds": 9,
                    "workspaceMutationCount": 0,
                    "artifactCount": 0,
                    "requiredDeliveryMissing": True,
                    "preDeliveryObservationCount": 6,
                    "consecutivePreDeliveryObservations": 6,
                    "actionNudgeCount": 4,
                    "lastActionNudgeObservationCount": 6,
                    "lastActionNudgeProviderRound": 7,
                },
            }
        ),
        encoding="utf-8",
    )
    checkpoint_path.with_name(
        f"{checkpoint_path.name}.handoff.json"
    ).write_text(
        json.dumps(
            {
                "schema": "zyra.typescript-runtime-handoff/v1",
                "task_id": "legacy-task",
                "run_id": "legacy-run",
                "source_session_id": "legacy-session",
                "counters": {},
                "progress": {
                    "workspaceMutationCount": 0,
                    "artifactCount": 0,
                    "requiredDeliveryMissing": True,
                },
                "recent_reasoning": [],
                "recent_tool_observations": [],
            }
        ),
        encoding="utf-8",
    )

    handoff = load_task_handoff_projection(
        runtime.execution_context.artifact_store.root,
        task_id="legacy-task",
        run_id="legacy-run",
        current_session_id="fresh-session",
    )

    assert handoff is not None
    assert handoff["progress"]["actionNudgeCount"] == 4
    assert handoff["inspection_continuity"] == {
        "requiredDeliveryMissing": True,
        "providerRounds": 9,
        "preDeliveryObservationCount": 6,
        "consecutivePreDeliveryObservations": 6,
        "actionNudgeCount": 4,
        "lastActionNudgeObservationCount": 6,
        "lastActionNudgeProviderRound": 7,
    }
    assert handoff["execution_continuity"] == {}


def test_cross_session_handoff_preserves_unverified_delivery_debt(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    checkpoint_root = (
        Path(runtime.execution_context.artifact_store.root) / ".runtime-checkpoints"
    )
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    common = {
        "schema": "zyra.typescript-runtime-handoff/v1",
        "task_id": "delivery-task",
        "run_id": "delivery-run",
        "authority_transfer": False,
        "claims_require_revalidation": True,
        "counters": {},
        "recent_reasoning": [],
        "recent_tool_observations": [],
    }
    delivered_path = checkpoint_root / "typescript-e01-delivered.json.handoff.json"
    delivered_path.write_text(
        json.dumps(
            {
                **common,
                "source_session_id": "delivered-session",
                "progress": {
                    "providerRounds": 12,
                    "realActionCount": 9,
                    "workspaceMutationCount": 3,
                    "verificationCount": 0,
                    "unresolvedVerificationScopes": ["shell:afctl:test:integration"],
                    "unresolvedVerificationFailures": [
                        {
                            "scope": "shell:afctl:test:integration",
                            "failedChecks": ["security"],
                            "failedCount": 1,
                            "failureKind": "reported_checks",
                            "attemptCount": 1,
                            "lastObservedWorkspaceMutationCount": 3,
                        }
                    ],
                    "verificationNudgeCount": 1,
                    "lastVerificationNudgeProviderRound": 12,
                    "artifactCount": 0,
                    "requiredDeliveryMissing": False,
                },
            }
        ),
        encoding="utf-8",
    )
    sparse_path = checkpoint_root / "typescript-e01-sparse-after-delivery.json.handoff.json"
    sparse_path.write_text(
        json.dumps(
            {
                **common,
                "source_session_id": "sparse-session",
                "progress": {
                    "providerRounds": 2,
                    "workspaceMutationCount": 0,
                    "verificationCount": 0,
                    "requiredDeliveryMissing": True,
                },
            }
        ),
        encoding="utf-8",
    )
    os.utime(delivered_path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(sparse_path, ns=(2_000_000_000, 2_000_000_000))

    handoff = load_task_handoff_projection(
        runtime.execution_context.artifact_store.root,
        task_id="delivery-task",
        run_id="delivery-run",
        current_session_id="fresh-session",
    )

    assert handoff is not None
    assert handoff["execution_continuity"] == {
        "requiredDeliveryMissing": False,
        "providerRounds": 12,
        "realActionCount": 9,
        "workspaceMutationCount": 3,
        "verificationCount": 0,
        "unresolvedVerificationScopes": ["shell:afctl:test:integration"],
        "unresolvedVerificationFailures": [
            {
                "scope": "shell:afctl:test:integration",
                "failedChecks": ["security"],
                "failedCount": 1,
                "failureKind": "reported_checks",
                "attemptCount": 1,
                "lastObservedWorkspaceMutationCount": 3,
            }
        ],
        "verificationNudgeCount": 1,
        "lastVerificationNudgeProviderRound": 12,
        "artifactCount": 0,
    }
    assert handoff["authority_transfer"] is False


def test_host_checkpoint_retries_transient_windows_replace_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    engine = TypeScriptClaudeQueryEngine(runtime.execution_context)
    session_id = "e04-host-checkpoint-transient-replace"
    real_replace = os.replace
    replace_attempts = 0
    retry_delays: list[float] = []

    def transient_replace(
        source: str | bytes | Path,
        target: str | bytes | Path,
    ) -> None:
        nonlocal replace_attempts
        replace_attempts += 1
        if replace_attempts < 3:
            raise PermissionError(5, "transient Windows sharing violation")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", transient_replace)
    monkeypatch.setattr(time, "sleep", retry_delays.append)

    committed = engine._persist_incremental_checkpoint(session_id, {"value": 1})

    assert committed["host_checkpoint_revision"] == 1
    assert replace_attempts == 4
    assert retry_delays == [0.05, 0.1]
    assert engine._checkpoint_path(session_id).exists()
    assert not tuple(engine._checkpoint_path(session_id).parent.glob("*.tmp"))


def test_host_checkpoint_replace_denial_exhaustion_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    engine = TypeScriptClaudeQueryEngine(runtime.execution_context)
    session_id = "e04-host-checkpoint-permanent-replace"
    original = engine._persist_incremental_checkpoint(session_id, {"value": 1})
    retry_delays: list[float] = []

    def denied_replace(source: str | bytes | Path, target: str | bytes | Path) -> None:
        raise PermissionError(5, "persistent Windows sharing violation")

    monkeypatch.setattr(os, "replace", denied_replace)
    monkeypatch.setattr(time, "sleep", retry_delays.append)

    with pytest.raises(PermissionError, match="persistent Windows sharing violation"):
        engine._persist_incremental_checkpoint(session_id, {"value": 2})

    persisted = json.loads(
        engine._checkpoint_path(session_id).read_text(encoding="utf-8")
    )
    assert (
        persisted["host_checkpoint_commit_id"]
        == original["host_checkpoint_commit_id"]
    )
    assert persisted["host_checkpoint_revision"] == 1
    assert retry_delays == [0.05, 0.1, 0.2, 0.4, 0.8, 1.6]
    assert not tuple(engine._checkpoint_path(session_id).parent.glob("*.tmp"))


def test_host_checkpoint_corruption_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    engine = TypeScriptClaudeQueryEngine(runtime.execution_context)
    session_id = "e04-host-checkpoint-corrupt"
    engine._checkpoint_path(session_id).write_text("{not-json", encoding="utf-8")
    with pytest.raises(TypeScriptRuntimeError) as captured:
        engine._load_incremental_checkpoint(session_id)
    assert captured.value.code == "typescript_runtime_checkpoint_corrupt"
