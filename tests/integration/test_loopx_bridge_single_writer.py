from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from zyra_integrations.loopx.bridge import (
    LoopXSingleWriter,
    SingleWriterConflictError,
    SingleWriterFenceLostError,
    WriterFence,
)


def _contending_writer(
    workspace: str,
    start_event,
    release_event,
    results,
) -> None:
    writer = LoopXSingleWriter(
        workspace_root=Path(workspace),
        writer_id=f"contender-{os.getpid()}",
    )
    start_event.wait(10)
    try:
        fence = writer.acquire(timeout_seconds=0.0)
    except SingleWriterConflictError as error:
        results.put(("conflict", error.code, 0))
        return
    results.put(("acquired", "", fence.fencing_epoch))
    release_event.wait(10)
    fence.release()


def _crashing_writer(workspace: str, connection) -> None:
    writer = LoopXSingleWriter(
        workspace_root=Path(workspace),
        writer_id=f"crashing-{os.getpid()}",
    )
    fence = writer.acquire()
    connection.send(fence.to_dict())
    connection.recv()
    os._exit(17)


def test_two_spawned_writers_allow_exactly_one_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    release = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_contending_writer,
            args=(str(workspace), start, release, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    observed = [results.get(timeout=15) for _ in processes]
    release.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert [item[0] for item in observed].count("acquired") == 1
    assert [item[0] for item in observed].count("conflict") == 1
    assert next(item for item in observed if item[0] == "conflict")[1] == (
        "loopx_writer_conflict"
    )


def test_live_writer_cannot_be_stolen_and_release_advances_epoch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    first = LoopXSingleWriter(
        workspace_root=workspace,
        writer_id="active-writer",
    )
    first_fence = first.acquire()
    second = LoopXSingleWriter(
        workspace_root=workspace,
        writer_id="second-writer",
    )
    with pytest.raises(SingleWriterConflictError) as conflict:
        second.acquire(timeout_seconds=0)
    assert conflict.value.code == "loopx_writer_conflict"
    owner = second.current_owner()
    assert owner["writer_id"] == "active-writer"
    assert owner["pid"] == os.getpid()

    first_fence.release()
    second_fence = second.acquire()
    try:
        assert second_fence.fencing_epoch > first_fence.fencing_epoch
        second_fence.heartbeat()
        second_fence.assert_owned()
    finally:
        second_fence.release()


def test_dead_process_lock_is_taken_over_and_old_fence_is_rejected(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_crashing_writer,
        args=(str(workspace), child),
    )
    process.start()
    stale = parent.recv()
    parent.send("crash-now")
    process.join(timeout=15)
    assert process.exitcode == 17
    lock_path = workspace / ".zyra" / "loopx" / "state" / "bridge" / "writer.lock"
    assert json.loads(lock_path.read_text(encoding="utf-8"))["fencing_token"] == stale[
        "fencing_token"
    ]

    replacement = LoopXSingleWriter(
        workspace_root=workspace,
        writer_id="replacement-writer",
    )
    new_fence = replacement.acquire()
    try:
        assert new_fence.fencing_epoch > int(stale["fencing_epoch"])
        assert new_fence.fencing_token != stale["fencing_token"]
        old_fence = WriterFence(
            owner=replacement,
            writer_id=str(stale["writer_id"]),
            fencing_epoch=int(stale["fencing_epoch"]),
            fencing_token=str(stale["fencing_token"]),
            pid=int(stale["pid"]),
            process_started_at=float(stale["process_started_at"]),
            acquired_at=float(stale["acquired_at_epoch"]),
        )
        with pytest.raises(SingleWriterFenceLostError):
            old_fence.assert_owned()
    finally:
        new_fence.release()
