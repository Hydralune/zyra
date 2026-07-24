from __future__ import annotations

import os
import sys
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping

import pytest


ROOT = Path(__file__).resolve().parents[2]
for package in (
    ROOT / "apps" / "api",
    ROOT / "packages" / "core",
    ROOT / "packages" / "workers",
):
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_workers.terminal import (  # noqa: E402
    PtyProcess,
    PtySpawnOptions,
    TerminalBinding,
    TerminalControlRequest,
    TerminalPermission,
    TerminalSessionRegistry,
    TerminalSpill,
    TerminalStateStore,
    TerminalTicketAuthority,
    pty_capabilities,
    spawn_pty,
)
from zyra_api.terminal_api import TerminalApiService  # noqa: E402


def collect_output(
    process: PtyProcess,
    chunks: list[bytes],
    completed: threading.Event,
) -> None:
    try:
        while True:
            chunk = process.read()
            if not chunk:
                return
            chunks.append(chunk)
    finally:
        completed.set()


def wait_for_output(
    chunks: list[bytes],
    marker: bytes,
    *,
    timeout: float = 8,
) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        material = b"".join(chunks)
        if marker in material:
            return material
        time.sleep(0.02)
    raise AssertionError(
        f"PTY output did not contain {marker!r}: {b''.join(chunks)!r}"
    )


def platform_command() -> tuple[str, str]:
    if os.name == "nt":
        return (
            os.environ.get("COMSPEC", "cmd.exe"),
            (
                "echo ZYRA_NATIVE_PTY_READY & "
                "set /p ZYRA_INPUT=Prompt: & "
                "call echo RECEIVED:%%ZYRA_INPUT%%"
            ),
        )
    return (
        os.environ.get("SHELL", "/bin/sh"),
        (
            "printf 'ZYRA_NATIVE_PTY_READY\\nPrompt: '; "
            "IFS= read -r ZYRA_INPUT; "
            "printf 'RECEIVED:%s\\n' \"$ZYRA_INPUT\""
        ),
    )


def long_running_command() -> tuple[str, str]:
    if os.name == "nt":
        return (
            os.environ.get("COMSPEC", "cmd.exe"),
            "echo ZYRA_KILL_READY & ping -n 30 127.0.0.1 >nul",
        )
    return (
        os.environ.get("SHELL", "/bin/sh"),
        "printf 'ZYRA_KILL_READY\\n'; sleep 30",
    )


def test_real_platform_pty_input_resize_and_exit(tmp_path: Path) -> None:
    shell, command = platform_command()
    process = spawn_pty(
        PtySpawnOptions(
            command=command,
            cwd=tmp_path.resolve(),
            shell=shell,
            rows=24,
            cols=80,
            environment=dict(os.environ),
        )
    )
    chunks: list[bytes] = []
    completed = threading.Event()
    reader = threading.Thread(
        target=collect_output,
        args=(process, chunks, completed),
        daemon=True,
    )
    reader.start()
    try:
        wait_for_output(chunks, b"ZYRA_NATIVE_PTY_READY")
        process.resize(32, 100)
        assert process.write(b"terminal-round-trip\r\n") == len(
            b"terminal-round-trip\r\n"
        )
        assert completed.wait(10)
        assert process.wait(timeout=2) == 0
        output = b"".join(chunks)
        assert b"Prompt:" in output
        assert b"terminal-round-trip" in output
        assert b"RECEIVED:" in output
    finally:
        if process.poll() is None:
            process.terminate_tree(0.1)
        process.close()
        reader.join(timeout=2)


def test_real_platform_pty_tree_kill_is_bounded(tmp_path: Path) -> None:
    shell, command = long_running_command()
    process = spawn_pty(
        PtySpawnOptions(
            command=command,
            cwd=tmp_path.resolve(),
            shell=shell,
            rows=20,
            cols=72,
            environment=dict(os.environ),
        )
    )
    chunks: list[bytes] = []
    completed = threading.Event()
    reader = threading.Thread(
        target=collect_output,
        args=(process, chunks, completed),
        daemon=True,
    )
    reader.start()
    try:
        wait_for_output(chunks, b"ZYRA_KILL_READY")
        started = time.monotonic()
        process.terminate_tree(grace_seconds=0.1)
        assert process.wait(timeout=3) is not None
        assert time.monotonic() - started < 4
    finally:
        if process.poll() is None:
            process.terminate_tree(0)
        process.close()
        reader.join(timeout=2)


def test_platform_driver_has_no_opaque_runtime_owner() -> None:
    capabilities = pty_capabilities()
    assert capabilities["driver"] in {"windows-conpty", "posix-pty"}
    assert capabilities["native_owner"] == "zyra_workers.terminal.drivers"
    assert capabilities["external_package"] is False
    assert capabilities["source_process"] is False
    assert capabilities["stdin"] is True
    assert capabilities["resize"] is True
    assert capabilities["process_tree_kill"] is True

    driver = (
        ROOT
        / "packages"
        / "workers"
        / "zyra_workers"
        / "terminal"
        / "drivers.py"
    ).read_text(encoding="utf-8")
    forbidden = ("node-pty", "pywinpty", "winpty", "../opencode", "../OpenHands")
    assert not any(value in driver for value in forbidden)


@pytest.mark.parametrize(
    ("command", "rows", "cols", "code"),
    [
        ("", 24, 80, "terminal_command_invalid"),
        ("echo invalid", 1, 80, "terminal_size_invalid"),
        ("echo invalid", 24, 1_001, "terminal_size_invalid"),
    ],
)
def test_platform_driver_rejects_invalid_spawn_before_process_creation(
    tmp_path: Path,
    command: str,
    rows: int,
    cols: int,
    code: str,
) -> None:
    from zyra_workers.terminal import TerminalError

    with pytest.raises(TerminalError) as rejected:
        spawn_pty(
            PtySpawnOptions(
                command=command,
                cwd=tmp_path.resolve(),
                shell="",
                rows=rows,
                cols=cols,
                environment=dict(os.environ),
            )
        )
    assert rejected.value.code == code


def test_fresh_terminal_api_state_owns_real_platform_pty(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fresh-terminal-state.json"
    artifact_root = tmp_path / "fresh-artifacts"
    events: list[dict[str, Any]] = []
    spills: list[TerminalSpill] = []

    def workspace(request: object) -> tuple[str, int, Path]:
        assert getattr(request, "task_id") == "task-fresh-terminal"
        return "workspace-fresh-terminal", 1, tmp_path.resolve()

    def permission(action: str, request: object) -> TerminalPermission:
        del request
        return TerminalPermission(
            effect="allow",
            decision_id=f"decision-{action}",
            reason_code=f"terminal.{action}.allow",
            reason=f"{action} allowed in fresh integration state",
        )

    def event(payload: Mapping[str, Any]) -> str:
        events.append(dict(payload))
        return f"event-{len(events)}"

    def spill_factory(binding: TerminalBinding):
        def spill(
            data: bytes,
            first_cursor: int,
            next_cursor: int,
            binary: bool,
            redacted: bool,
        ) -> TerminalSpill:
            import hashlib

            artifact_root.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(data).hexdigest()
            (artifact_root / f"{digest}.spill").write_bytes(data)
            value = TerminalSpill(
                artifact_id=f"artifact-{digest[:16]}",
                revision=f"sha256:{digest}",
                media_type=(
                    "application/octet-stream"
                    if binary
                    else "text/plain"
                ),
                sha256=digest,
                byte_length=next_cursor - first_cursor,
                first_cursor=first_cursor,
                next_cursor=next_cursor,
                binary=binary,
                redacted=redacted,
            )
            assert binding.task_id == "task-fresh-terminal"
            spills.append(value)
            return value

        return spill

    assert not state_path.exists()
    assert not artifact_root.exists()
    registry = TerminalSessionRegistry(
        state_store=TerminalStateStore(state_path),
        workspace_resolver=workspace,
        permission_port=permission,
        event_sink=event,
        spill_sink_factory=spill_factory,
        ticket_authority=TerminalTicketAuthority(
            b"fresh-terminal-integration-key!!",
            ttl_seconds=20,
        ),
        maximum_sessions=2,
    )
    api = TerminalApiService(registry)
    shell, command_value = platform_command()
    try:
        created = api.create(
            task_id="task-fresh-terminal",
            run_id="run-fresh-terminal",
            payload={
                "session_id": "session-fresh-terminal",
                "worker_id": "worker-fresh-terminal",
                "command_id": "command-fresh-terminal",
                "tool_call_id": "tool-fresh-terminal",
                "span_id": "span-fresh-terminal",
                "command": command_value,
                "shell": shell,
                "cwd": ".",
                "rows": 24,
                "cols": 80,
                "environment": {},
            },
            actor_id="operator-fresh-terminal",
        )
        assert created.status == HTTPStatus.CREATED
        terminal = created.body["terminal"]
        terminal_id = terminal["binding"]["terminal_id"]
        session = registry.get("task-fresh-terminal", terminal_id)
        deadline = time.monotonic() + 8
        while b"ZYRA_NATIVE_PTY_READY" not in "".join(
            chunk.text for chunk in session.journal.replay(0).chunks
        ).encode("utf-8"):
            if time.monotonic() >= deadline:
                raise AssertionError("fresh API PTY did not become ready")
            time.sleep(0.02)
        ticket = api.ticket(
            task_id="task-fresh-terminal",
            terminal_id=terminal_id,
            run_id="run-fresh-terminal",
            payload={
                "session_id": "session-fresh-terminal",
                "cursor": 0,
                "protocol": "zyra.terminal.v1",
            },
            origin="https://console.example.test",
        )
        assert ticket.status == HTTPStatus.OK
        assert ticket.body["ticket"]
        assert ticket.body["receipt"]["operation"] == "terminal_ticket_issue"
        assert ticket.body["receipt"]["terminal_id"] == terminal_id
        assert ticket.body["receipt"]["cursor"] == 0
        registry.control(
            TerminalControlRequest(
                task_id="task-fresh-terminal",
                run_id="run-fresh-terminal",
                terminal_id=terminal_id,
                session_id="session-fresh-terminal",
                worker_id="worker-fresh-terminal",
                tool_call_id="tool-fresh-terminal",
                span_id="span-fresh-terminal",
                actor_id="operator-fresh-terminal",
                action="resize",
                sequence=1,
                sealed=False,
                competition_mode="interactive",
                rows=31,
                cols=99,
            )
        )
        registry.control(
            TerminalControlRequest(
                task_id="task-fresh-terminal",
                run_id="run-fresh-terminal",
                terminal_id=terminal_id,
                session_id="session-fresh-terminal",
                worker_id="worker-fresh-terminal",
                tool_call_id="tool-fresh-terminal",
                span_id="span-fresh-terminal",
                actor_id="operator-fresh-terminal",
                action="input",
                sequence=1,
                sealed=False,
                competition_mode="interactive",
                data="fresh-api-round-trip\r\n",
            )
        )
        deadline = time.monotonic() + 10
        while session.status.phase.value == "running":
            if time.monotonic() >= deadline:
                raise AssertionError("fresh API PTY did not exit")
            time.sleep(0.02)
        output = "".join(
            chunk.text
            for chunk in session.journal.replay(0).chunks
        )
        assert "fresh-api-round-trip" in output
        assert session.status.phase.value == "exited"
        assert session.status.rows == 31
        assert session.status.cols == 99
        assert state_path.exists()
        assert any(
            item.get("event_type") == "terminal.created"
            for item in events
        )
        assert any(
            item.get("event_type") == "terminal.input"
            for item in events
        )
        assert spills == []
    finally:
        registry.shutdown()
