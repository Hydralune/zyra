from __future__ import annotations

import hashlib
import queue
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
    SecretRedactor,
    TerminalBinding,
    TerminalControlRequest,
    TerminalCreateRequest,
    TerminalError,
    TerminalOutputJournal,
    TerminalPermission,
    TerminalPhase,
    TerminalSessionRegistry,
    TerminalSpill,
    TerminalStateStore,
    TerminalStatus,
    TerminalTicketAuthority,
)
from zyra_api.terminal_api import TerminalApiService  # noqa: E402


class FakePty(PtyProcess):
    def __init__(self, options: PtySpawnOptions, *, output: bytes = b"") -> None:
        self.options = options
        self._pid = 42_001
        self._exit_code: int | None = None
        self._closed = False
        self._output: queue.Queue[bytes] = queue.Queue()
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.terminated = 0
        if output:
            self._output.put(output)

    @property
    def pid(self) -> int:
        return self._pid

    def read(self, maximum: int = 65_536) -> bytes:
        del maximum
        return self._output.get(timeout=5)

    def write(self, data: bytes) -> int:
        if self._exit_code is not None:
            raise TerminalError("terminal_pty_closed", "fake PTY is closed")
        material = bytes(data)
        self.writes.append(material)
        self._output.put(b"observed:" + material)
        return len(material)

    def resize(self, rows: int, cols: int) -> None:
        if self._exit_code is not None:
            raise TerminalError("terminal_pty_closed", "fake PTY is closed")
        self.resizes.append((rows, cols))

    def poll(self) -> int | None:
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.monotonic() + (5 if timeout is None else timeout)
        while self._exit_code is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("fake PTY is still running")
            time.sleep(0.005)
        return self._exit_code

    def terminate_tree(self, grace_seconds: float = 1.0) -> None:
        del grace_seconds
        self.terminated += 1
        if self._exit_code is None:
            self._exit_code = 137
            self._output.put(b"")

    def exit(self, code: int = 0, output: bytes = b"") -> None:
        if output:
            self._output.put(output)
        self._exit_code = code
        self._output.put(b"")

    def close(self) -> None:
        self._closed = True
        if self._exit_code is None:
            self._exit_code = 0
            self._output.put(b"")


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        effects: Mapping[str, str] | None = None,
        initial_output: bytes = b"",
        enabled: bool = True,
    ) -> None:
        self.tmp_path = tmp_path
        self.effects = dict(effects or {})
        self.initial_output = initial_output
        self.permission_calls: list[tuple[str, object]] = []
        self.spawned: list[FakePty] = []
        self.events: list[dict[str, Any]] = []
        self.spills: list[TerminalSpill] = []

        def workspace(
            request: TerminalCreateRequest,
        ) -> tuple[str, int, Path]:
            assert request.task_id == "task-terminal"
            return "workspace-terminal", 7, tmp_path

        def permission(
            action: str,
            request: TerminalCreateRequest | TerminalControlRequest,
        ) -> TerminalPermission:
            self.permission_calls.append((action, request))
            effect = self.effects.get(action, "allow")
            return TerminalPermission(
                effect=effect,
                decision_id=f"decision-{action}-{effect}",
                request_id=f"request-{action}" if effect == "ask" else "",
                permit_id=request.permission_permit_id,
                reason_code=f"permission.{action}.{effect}",
                reason=f"{action} was {effect}",
            )

        def spawn(options: PtySpawnOptions) -> PtyProcess:
            process = FakePty(options, output=self.initial_output)
            self.spawned.append(process)
            return process

        def event(payload: Mapping[str, Any]) -> str:
            selected = dict(payload)
            self.events.append(selected)
            return f"event-{len(self.events)}"

        def spill_factory(binding: TerminalBinding):
            def spill(
                data: bytes,
                first_cursor: int,
                next_cursor: int,
                binary: bool,
                redacted: bool,
            ) -> TerminalSpill:
                digest = hashlib.sha256(data).hexdigest()
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
                assert binding.task_id == "task-terminal"
                self.spills.append(value)
                return value

            return spill

        self.registry = TerminalSessionRegistry(
            state_store=TerminalStateStore(tmp_path / "terminal-state.json"),
            workspace_resolver=workspace,
            permission_port=permission,
            event_sink=event,
            spill_sink_factory=spill_factory,
            ticket_authority=TerminalTicketAuthority(
                b"t" * 32,
                ttl_seconds=20,
            ),
            pty_spawner=spawn,
            maximum_sessions=4,
            enabled=enabled,
        )

    def create_request(
        self,
        *,
        sealed: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> TerminalCreateRequest:
        return TerminalCreateRequest(
            task_id="task-terminal",
            run_id="run-terminal",
            session_id="session-terminal",
            worker_id="worker-terminal",
            command_id="command-terminal",
            tool_call_id="tool-terminal",
            span_id="span-terminal",
            actor_id="operator",
            command="python -i",
            title="Terminal",
            cwd=".",
            shell="",
            rows=24,
            cols=80,
            sealed=sealed,
            competition_mode="interactive",
            environment=dict(environment or {}),
            correlation_id="correlation-terminal",
            causation_id="cause-terminal",
        )

    @staticmethod
    def control(
        terminal_id: str,
        action: str,
        *,
        sequence: int,
        data: str = "",
        rows: int = 0,
        cols: int = 0,
        sealed: bool = False,
        run_id: str = "run-terminal",
    ) -> TerminalControlRequest:
        return TerminalControlRequest(
            task_id="task-terminal",
            run_id=run_id,
            terminal_id=terminal_id,
            session_id="session-terminal",
            worker_id="worker-terminal",
            tool_call_id="tool-terminal",
            span_id="span-terminal",
            actor_id="operator",
            action=action,
            sequence=sequence,
            data=data,
            rows=rows,
            cols=cols,
            reason="test",
            sealed=sealed,
            competition_mode="interactive",
        )


def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.01)


def binding() -> TerminalBinding:
    return TerminalBinding(
        task_id="task-terminal",
        run_id="run-terminal",
        terminal_id="terminal-one",
        session_id="session-terminal",
        workspace_id="workspace-terminal",
        workspace_revision=7,
        worker_id="worker-terminal",
        command_id="command-terminal",
        tool_call_id="tool-terminal",
        span_id="span-terminal",
    )


def test_terminal_status_rejects_non_canonical_transition() -> None:
    status = TerminalStatus(
        phase=TerminalPhase.RUNNING,
        cwd=".",
        title="terminal",
        rows=24,
        cols=80,
        cursor=0,
        earliest_cursor=0,
        started_at="2026-07-24T00:00:00+00:00",
        updated_at="2026-07-24T00:00:00+00:00",
        state_mutation_id="mutation-one",
    )
    exited = status.transition(
        TerminalPhase.EXITED,
        state_mutation_id="mutation-two",
        exit_code=0,
    )
    assert exited.phase == TerminalPhase.EXITED
    assert exited.exit_code == 0
    with pytest.raises(TerminalError, match="cannot transition"):
        exited.transition(
            TerminalPhase.RUNNING,
            state_mutation_id="mutation-three",
        )


def test_ticket_is_short_lived_origin_bound_and_one_use() -> None:
    clock = [1_000.0]
    authority = TerminalTicketAuthority(
        b"k" * 32,
        ttl_seconds=5,
        clock=lambda: clock[0],
    )
    ticket = authority.issue(
        binding=binding(),
        origin="https://console.example.test/path",
        cursor=91,
    )
    consumed = authority.consume(
        ticket.token,
        expected_origin="https://console.example.test",
        expected_task_id="task-terminal",
        expected_terminal_id="terminal-one",
        expected_protocol="zyra.terminal.v1",
    )
    assert consumed.cursor == 91
    with pytest.raises(TerminalError) as replay:
        authority.consume(
            ticket.token,
            expected_origin="https://console.example.test",
            expected_task_id="task-terminal",
            expected_terminal_id="terminal-one",
            expected_protocol="zyra.terminal.v1",
        )
    assert replay.value.code == "terminal_ticket_replayed"

    other = authority.issue(
        binding=binding(),
        origin="https://console.example.test",
        cursor=0,
    )
    with pytest.raises(TerminalError) as mismatch:
        authority.consume(
            other.token,
            expected_origin="https://evil.example.test",
            expected_task_id="task-terminal",
            expected_terminal_id="terminal-one",
            expected_protocol="zyra.terminal.v1",
        )
    assert mismatch.value.code == "terminal_ticket_origin_mismatch"

    expired = authority.issue(
        binding=binding(),
        origin="https://console.example.test",
        cursor=0,
    )
    clock[0] += 6
    with pytest.raises(TerminalError) as expiry:
        authority.consume(
            expired.token,
            expected_origin="https://console.example.test",
            expected_task_id="task-terminal",
            expected_terminal_id="terminal-one",
            expected_protocol="zyra.terminal.v1",
        )
    assert expiry.value.code == "terminal_ticket_expired"


def test_output_journal_redacts_spills_binary_and_resyncs() -> None:
    spills: list[TerminalSpill] = []
    spill_material: list[bytes] = []

    def spill(
        data: bytes,
        first: int,
        next_cursor: int,
        binary: bool,
        redacted: bool,
    ) -> TerminalSpill:
        spill_material.append(bytes(data))
        digest = hashlib.sha256(data).hexdigest()
        value = TerminalSpill(
            artifact_id=f"artifact-{len(spills)}",
            revision=digest,
            media_type="application/octet-stream" if binary else "text/plain",
            sha256=digest,
            byte_length=next_cursor - first,
            first_cursor=first,
            next_cursor=next_cursor,
            binary=binary,
            redacted=redacted,
        )
        spills.append(value)
        return value

    journal = TerminalOutputJournal(
        maximum_memory_bytes=1_024,
        maximum_chunk_bytes=256,
        spill_sink=spill,
        redactor=SecretRedactor(["exact-secret"]),
    )
    chunks = journal.append(
        b"password=hunter2 exact-secret\n"
        + b"x" * 1_400
        + b"\x00\x01\x02binary exact-secret"
    )
    assert chunks[0].redacted is True
    assert "[REDACTED]" in chunks[0].text
    assert journal.cursor > 900
    assert journal.binary_bytes > 0
    assert spills
    assert any(item.binary for item in spills)
    assert all(b"exact-secret" not in material for material in spill_material)
    assert any(b"[REDACTED]" in material for material in spill_material)

    stale = journal.replay(0)
    assert stale.reason == "stale_cursor"
    assert stale.accepted_cursor == journal.earliest_cursor
    future = journal.replay(journal.cursor + 100)
    assert future.reason == "future_cursor"
    assert future.accepted_cursor == journal.cursor


def test_output_redaction_holds_credentials_split_across_pty_reads() -> None:
    journal = TerminalOutputJournal(
        maximum_memory_bytes=1_024,
        maximum_chunk_bytes=256,
        redactor=SecretRedactor(["exact-secret"]),
    )
    prefix = journal.append(b"prefix password=hun")
    assert "".join(chunk.text for chunk in prefix) == "prefix "
    first = journal.append(b"ter2\nsecret=exact-")
    assert first
    assert "hunter2" not in "".join(chunk.text for chunk in first)
    assert "[REDACTED]" in "".join(chunk.text for chunk in first)
    assert journal.snapshot()["pending_redaction_bytes"] > 0
    final = journal.append(b"secret\n")
    material = "".join(chunk.text for chunk in final)
    assert "exact-secret" not in material
    assert "[REDACTED]" in material
    assert journal.close() == ()


def test_output_redaction_preserves_cursor_bytes_and_internal_chunk_boundaries() -> None:
    short = TerminalOutputJournal(
        maximum_memory_bytes=1_024,
        maximum_chunk_bytes=256,
        redactor=SecretRedactor(["abcd"]),
    )
    short_chunks = short.append(b"abcd\n")
    assert len(short_chunks) == 1
    assert short_chunks[0].redacted is True
    assert "abcd" not in short_chunks[0].text
    assert len(short_chunks[0].text.encode("utf-8")) == short_chunks[0].byte_length
    assert short_chunks[0].sha256 == hashlib.sha256(
        short_chunks[0].text.encode("utf-8")
    ).hexdigest()
    assert short_chunks[0].sha256 != hashlib.sha256(b"abcd\n").hexdigest()

    boundary = TerminalOutputJournal(
        maximum_memory_bytes=2_048,
        maximum_chunk_bytes=256,
        redactor=SecretRedactor(["boundary-secret"]),
    )
    boundary_chunks = boundary.append(
        b"x" * 250
        + b"boundary-secret"
        + b"\n"
    )
    rendered = "".join(chunk.text for chunk in boundary_chunks)
    assert len(boundary_chunks) == 2
    assert "boundary-secret" not in rendered
    assert all(chunk.redacted for chunk in boundary_chunks)
    assert all(
        len(chunk.text.encode("utf-8")) == chunk.byte_length
        for chunk in boundary_chunks
    )


def test_output_frames_preserve_utf8_boundaries_and_bound_binary_placeholders() -> None:
    journal = TerminalOutputJournal(
        maximum_memory_bytes=2_048,
        maximum_chunk_bytes=256,
    )
    euro = "€".encode()
    assert journal.append(euro[:2]) == ()
    completed = journal.append(euro[2:] + b"\n")
    assert len(completed) == 1
    assert completed[0].text == "€\n"
    assert len(completed[0].text.encode("utf-8")) == completed[0].byte_length

    boundary = journal.append(b"x" * 255 + euro + b"\n")
    assert len(boundary) == 2
    assert boundary[0].text == "x" * 255
    assert boundary[1].text == "€\n"
    assert all(
        len(chunk.text.encode("utf-8")) <= chunk.byte_length
        for chunk in boundary
    )

    binary = journal.append(b"\x00")
    assert len(binary) == 1
    assert binary[0].text
    assert len(binary[0].text.encode("utf-8")) <= binary[0].byte_length
    assert journal.binary_bytes == 1
    assert journal.snapshot()["spills"][-1]["binary"] is True


def test_registry_routes_create_input_resize_kill_and_events(tmp_path: Path) -> None:
    harness = Harness(tmp_path, initial_output=b"ready> ")
    projection = harness.registry.create(harness.create_request())
    terminal_id = projection.binding.terminal_id
    assert projection.status.phase == TerminalPhase.RUNNING
    assert projection.binding.workspace_id == "workspace-terminal"
    assert harness.permission_calls[0][0] == "create"
    assert len(harness.spawned) == 1

    wait_until(lambda: harness.registry.get(
        "task-terminal",
        terminal_id,
    ).journal.cursor >= len(b"ready> "))

    result = harness.registry.control(
        harness.control(
            terminal_id,
            "input",
            sequence=1,
            data="echo hello\r",
        )
    )
    assert result["accepted"] is True
    assert result["written"] == len("echo hello\r")
    assert harness.spawned[0].writes == [b"echo hello\r"]
    wait_until(
        lambda: harness.registry.get(
            "task-terminal",
            terminal_id,
        ).journal.cursor > len(b"ready> ")
    )

    harness.registry.control(
        harness.control(
            terminal_id,
            "resize",
            sequence=1,
            rows=40,
            cols=120,
        )
    )
    assert harness.spawned[0].resizes == [(40, 120)]
    assert harness.registry.get(
        "task-terminal",
        terminal_id,
    ).status.resize_sequence == 1

    killed = harness.registry.control(
        harness.control(terminal_id, "kill", sequence=2)
    )
    assert killed["accepted"] is True
    assert killed["status"]["phase"] == "killed"
    assert harness.spawned[0].terminated == 1
    wait_until(
        lambda: any(
            item["event_type"] == "terminal.exited"
            for item in harness.events
        )
    )
    assert {
        item["event_type"]
        for item in harness.events
    } >= {
        "terminal.created",
        "terminal.output",
        "terminal.input",
        "terminal.resize",
        "terminal.killed",
        "terminal.exited",
    }
    state = harness.registry.state_store.load()[terminal_id]
    assert state["canonical_owner"] == (
        "zyra_workers.terminal.TerminalSessionRegistry"
    )
    harness.registry.shutdown()


def test_duplicate_sequences_and_binding_mismatch_do_not_touch_pty(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    projection = harness.registry.create(harness.create_request())
    terminal_id = projection.binding.terminal_id
    harness.registry.control(
        harness.control(
            terminal_id,
            "input",
            sequence=1,
            data="first",
        )
    )
    with pytest.raises(TerminalError) as duplicate:
        harness.registry.control(
            harness.control(
                terminal_id,
                "input",
                sequence=1,
                data="duplicate",
            )
        )
    assert duplicate.value.code == "terminal_input_sequence_stale"
    with pytest.raises(TerminalError) as binding_error:
        harness.registry.control(
            harness.control(
                terminal_id,
                "input",
                sequence=2,
                data="wrong run",
                run_id="another-run",
            )
        )
    assert binding_error.value.code == "terminal_control_binding_mismatch"
    assert harness.spawned[0].writes == [b"first"]
    harness.registry.shutdown()


@pytest.mark.parametrize("effect", ["ask", "deny"])
def test_create_permission_never_spawns_when_not_allowed(
    tmp_path: Path,
    effect: str,
) -> None:
    harness = Harness(tmp_path, effects={"create": effect})
    projection = harness.registry.create(harness.create_request())
    expected = (
        TerminalPhase.PERMISSION_PENDING
        if effect == "ask"
        else TerminalPhase.REJECTED
    )
    assert projection.status.phase == expected
    assert projection.human_intervention_count == 0
    assert not harness.spawned
    assert harness.registry.snapshot()["sessions"] == {}


@pytest.mark.parametrize("action", ["input", "kill"])
def test_control_ask_returns_retryable_exact_permission_request(
    tmp_path: Path,
    action: str,
) -> None:
    harness = Harness(tmp_path, effects={action: "ask"})
    projection = harness.registry.create(harness.create_request())
    request = harness.control(
        projection.binding.terminal_id,
        action,
        sequence=1,
        data="approval-gated input" if action == "input" else "",
    )
    with pytest.raises(TerminalError) as pending:
        harness.registry.control(request)
    assert pending.value.status == 202
    assert pending.value.retryable is True
    assert pending.value.details["permission"]["effect"] == "ask"
    assert pending.value.details["permission"]["request_id"] == (
        f"request-{action}"
    )
    assert f"request-{action}" in str(pending.value)
    assert harness.spawned[0].writes == []
    assert harness.spawned[0].terminated == 0
    harness.registry.shutdown()


def test_sealed_create_and_input_reject_without_permission_owner_call(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    denied = harness.registry.create(harness.create_request(sealed=True))
    assert denied.status.phase == TerminalPhase.REJECTED
    assert denied.permission.reason_code == "terminal.sealed_human_input_denied"
    assert harness.permission_calls == []
    assert harness.spawned == []

    allowed = harness.registry.create(harness.create_request())
    harness.permission_calls.clear()
    with pytest.raises(TerminalError) as sealed_input:
        harness.registry.control(
            harness.control(
                allowed.binding.terminal_id,
                "input",
                sequence=1,
                data="must not reach PTY",
                sealed=True,
            )
        )
    assert sealed_input.value.code == "terminal_input_permission_denied"
    assert harness.permission_calls == []
    assert harness.spawned[0].writes == []
    harness.registry.shutdown()


def test_permission_owner_and_runtime_disable_are_fail_closed(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, enabled=False)
    with pytest.raises(TerminalError) as disabled:
        harness.registry.create(harness.create_request())
    assert disabled.value.code == "terminal_runtime_disabled"

    enabled = Harness(tmp_path / "owner")

    def wrong_owner(
        action: str,
        request: TerminalCreateRequest | TerminalControlRequest,
    ) -> TerminalPermission:
        del action, request
        return TerminalPermission(
            effect="allow",
            decision_id="bad",
            reason_code="bad",
            reason="bad",
            canonical_owner="python.LocalPermission",
        )

    enabled.registry.permission_port = wrong_owner
    with pytest.raises(TerminalError) as owner:
        enabled.registry.create(enabled.create_request())
    assert owner.value.code == "terminal_permission_owner_invalid"
    assert enabled.spawned == []


def test_ticket_binding_reconnect_and_viewer_detach_do_not_kill(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    projection = harness.registry.create(harness.create_request())
    issued = harness.registry.ticket(
        task_id="task-terminal",
        terminal_id=projection.binding.terminal_id,
        run_id="run-terminal",
        session_id="session-terminal",
        origin="https://console.example.test",
        cursor=0,
        protocol="zyra.terminal.v1",
    )
    session, ticket, connection_id = harness.registry.consume_ticket(
        issued.ticket,
        origin="https://console.example.test",
        task_id="task-terminal",
        terminal_id=projection.binding.terminal_id,
        protocol="zyra.terminal.v1",
    )
    assert ticket.binding == projection.binding
    assert session.status.viewers == 1
    session.detach(connection_id)
    assert session.status.viewers == 0
    assert session.status.phase == TerminalPhase.RUNNING
    assert harness.spawned[0].terminated == 0
    harness.registry.shutdown()


def test_recovery_marks_persisted_live_pty_as_orphan(tmp_path: Path) -> None:
    path = tmp_path / "terminal-state.json"
    store = TerminalStateStore(path)
    store.save(
        {
            "terminal-orphan": {
                "status": {"phase": "running"},
                "binding": {"task_id": "task-terminal"},
            },
            "terminal-complete": {
                "status": {"phase": "exited"},
                "binding": {"task_id": "task-terminal"},
            },
        }
    )
    harness = Harness(tmp_path)
    snapshot = harness.registry.snapshot()
    assert snapshot["recovered_orphans"] == [
        {
            "terminal_id": "terminal-orphan",
            "previous_phase": "running",
            "recovered_phase": "crashed",
            "reason": "api_process_restarted_without_live_pty_owner",
        }
    ]
    assert store.load() == {}


def test_environment_filters_credentials_before_spawn(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(TerminalError) as rejected:
        harness.registry.create(
            harness.create_request(
                environment={"API_TOKEN": "must-not-enter-child"}
            )
        )
    assert rejected.value.code == "terminal_environment_rejected"
    assert harness.spawned == []

    accepted = harness.registry.create(
        harness.create_request(environment={"ZYRA_VISIBLE": "yes"})
    )
    assert accepted.status.phase == TerminalPhase.RUNNING
    assert harness.spawned[0].options.environment["ZYRA_VISIBLE"] == "yes"
    harness.registry.shutdown()


def test_terminal_api_service_create_list_ticket_get_and_kill(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    api = TerminalApiService(harness.registry)
    payload = {
        "session_id": "session-terminal",
        "worker_id": "worker-terminal",
        "command_id": "command-terminal",
        "tool_call_id": "tool-terminal",
        "span_id": "span-terminal",
        "command": "python -i",
        "title": "API terminal",
        "cwd": ".",
        "rows": 28,
        "cols": 96,
        "sealed": False,
        "competition_mode": "interactive",
        "environment": {"ZYRA_TEST_VALUE": "visible"},
        "correlation_id": "correlation-terminal",
        "causation_id": "cause-terminal",
    }
    created = api.create(
        task_id="task-terminal",
        run_id="run-terminal",
        payload=payload,
        actor_id="operator",
    )
    assert created.status == HTTPStatus.CREATED
    terminal = created.body["terminal"]
    terminal_id = terminal["binding"]["terminal_id"]
    assert terminal["status"]["phase"] == "running"
    assert created.headers["Cache-Control"] == "no-store, max-age=0"

    listed = api.list(task_id="task-terminal", include_closed=False)
    assert listed.status == HTTPStatus.OK
    assert listed.body["count"] == 1
    selected = api.get(task_id="task-terminal", terminal_id=terminal_id)
    assert selected.body["terminal"]["binding"]["workspace_id"] == (
        "workspace-terminal"
    )
    ticket = api.ticket(
        task_id="task-terminal",
        terminal_id=terminal_id,
        run_id="run-terminal",
        payload={
            "session_id": "session-terminal",
            "cursor": 0,
            "protocol": "zyra.terminal.v1",
        },
        origin="https://console.example.test",
    )
    assert ticket.status == HTTPStatus.OK
    assert ticket.body["ticket"]
    assert ticket.body["socket_path"].endswith(f"/{terminal_id}/connect")
    receipt = ticket.body["receipt"]
    assert receipt["schema"] == "zyra.terminal-ticket-receipt.v1"
    assert receipt["operation"] == "terminal_ticket_issue"
    assert receipt["outcome"] == "issued"
    assert receipt["task_id"] == "task-terminal"
    assert receipt["terminal_id"] == terminal_id
    assert receipt["cursor"] == 0
    assert len(receipt["ticket_sha256"]) == 64
    assert ticket.body["ticket"] not in receipt["ticket_sha256"]

    killed = api.kill(
        task_id="task-terminal",
        terminal_id=terminal_id,
        run_id="run-terminal",
        payload={
            "session_id": "session-terminal",
            "worker_id": "worker-terminal",
            "tool_call_id": "tool-terminal",
            "span_id": "span-terminal",
            "sequence": 1,
            "reason": "explicit API test cleanup",
            "sealed": False,
        },
        actor_id="operator",
    )
    assert killed.status == HTTPStatus.OK
    assert killed.body["terminal"]["status"]["phase"] == "killed"
    assert harness.spawned[0].terminated == 1
    harness.registry.shutdown()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("sealed", "maybe", "terminal_sealed_invalid"),
        ("rows", 1, "terminal_rows_invalid"),
        ("environment", {"A": "\x00"}, "terminal_environment_value_invalid"),
    ],
)
def test_terminal_api_service_rejects_invalid_payload_before_spawn(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    harness = Harness(tmp_path)
    api = TerminalApiService(harness.registry)
    payload: dict[str, object] = {
        "session_id": "session-terminal",
        "worker_id": "worker-terminal",
        "command_id": "command-terminal",
        "tool_call_id": "tool-terminal",
        "span_id": "span-terminal",
        "command": "python -i",
        field: value,
    }
    with pytest.raises(TerminalError) as rejected:
        api.create(
            task_id="task-terminal",
            run_id="run-terminal",
            payload=payload,
            actor_id="operator",
        )
    assert rejected.value.code == code
    assert harness.spawned == []


def test_create_event_failure_cleans_process_registry_and_state(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    recorded: list[str] = []

    def fail_created(payload: Mapping[str, Any]) -> str:
        event_type = str(payload.get("event_type") or "")
        recorded.append(event_type)
        if event_type == "terminal.created":
            raise RuntimeError("canonical event spine unavailable")
        return f"event-{len(recorded)}"

    harness.registry.event_sink = fail_created
    with pytest.raises(RuntimeError, match="event spine unavailable"):
        harness.registry.create(harness.create_request())
    assert harness.spawned[0].terminated == 1
    assert harness.registry.list("task-terminal", include_closed=True) == ()
    assert TerminalStateStore(tmp_path / "terminal-state.json").load() == {}


def test_multiple_session_output_persists_without_cross_session_locking(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    first = harness.registry.create(harness.create_request())
    second = harness.registry.create(harness.create_request())
    assert first.binding.terminal_id != second.binding.terminal_id

    harness.spawned[0]._output.put(b"first-session-output")
    harness.spawned[1]._output.put(b"second-session-output")
    wait_until(
        lambda: all(
            harness.registry.get(
                "task-terminal",
                terminal_id,
            ).status.cursor > 0
            for terminal_id in (
                first.binding.terminal_id,
                second.binding.terminal_id,
            )
        ),
    )
    snapshot = harness.registry.snapshot()
    assert set(snapshot["sessions"]) == {
        first.binding.terminal_id,
        second.binding.terminal_id,
    }
    persisted = TerminalStateStore(tmp_path / "terminal-state.json").load()
    assert persisted[first.binding.terminal_id]["status"]["cursor"] > 0
    assert persisted[second.binding.terminal_id]["status"]["cursor"] > 0
    harness.registry.shutdown()


def test_terminal_input_has_server_side_byte_rate_budget(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    projection = harness.registry.create(harness.create_request())
    terminal_id = projection.binding.terminal_id
    material = "x" * (128 * 1_024)
    for sequence in (1, 2):
        result = harness.registry.control(
            harness.control(
                terminal_id,
                "input",
                sequence=sequence,
                data=material,
            )
        )
        assert result["accepted"] is True
    with pytest.raises(TerminalError) as limited:
        harness.registry.control(
            harness.control(
                terminal_id,
                "input",
                sequence=3,
                data=material,
            )
        )
    assert limited.value.code == "terminal_input_rate_limited"
    assert limited.value.status == 429
    assert limited.value.retryable is True
    assert limited.value.details["retry_after_ms"] > 0
    assert len(harness.spawned[0].writes) == 2
    harness.registry.shutdown()
