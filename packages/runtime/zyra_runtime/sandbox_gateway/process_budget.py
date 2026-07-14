from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Mapping

from .models import CommandBudget, ProcessOutput


@dataclass(frozen=True, slots=True)
class StreamChunk:
    stream: str
    content: bytes
    sequence: int
    received_at: float


@dataclass(frozen=True, slots=True)
class OutputBudgetState:
    stdout_bytes: int
    stderr_bytes: int
    combined_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    combined_truncated: bool

    @property
    def exceeded(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated or self.combined_truncated

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "combined_bytes": self.combined_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "combined_truncated": self.combined_truncated,
            "exceeded": self.exceeded,
        }


class OutputBudgetCollector:
    def __init__(
        self,
        budget: CommandBudget,
        *,
        on_chunk: Callable[[StreamChunk], None] | None = None,
    ) -> None:
        self.budget = budget
        self.on_chunk = on_chunk
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._stdout_seen = 0
        self._stderr_seen = 0
        self._stdout_truncated = False
        self._stderr_truncated = False
        self._combined_truncated = False
        self._sequence = 0
        self._lock = threading.RLock()

    def append(self, stream: str, content: bytes) -> OutputBudgetState:
        if stream not in {"stdout", "stderr"}:
            raise ValueError(f"unknown process stream: {stream}")
        data = bytes(content)
        with self._lock:
            self._sequence += 1
            if stream == "stdout":
                self._stdout_seen += len(data)
            else:
                self._stderr_seen += len(data)
            combined_seen = self._stdout_seen + self._stderr_seen
            stream_limit = (
                self.budget.stdout_limit_bytes
                if stream == "stdout"
                else self.budget.stderr_limit_bytes
            )
            buffer = self._stdout if stream == "stdout" else self._stderr
            remaining_stream = max(0, stream_limit - len(buffer))
            remaining_combined = max(
                0,
                self.budget.combined_output_limit_bytes
                - len(self._stdout)
                - len(self._stderr),
            )
            accepted = data[: min(remaining_stream, remaining_combined)]
            buffer.extend(accepted)
            if len(data) > remaining_stream:
                if stream == "stdout":
                    self._stdout_truncated = True
                else:
                    self._stderr_truncated = True
            if len(data) > remaining_combined or combined_seen > self.budget.combined_output_limit_bytes:
                self._combined_truncated = True
            chunk = StreamChunk(
                stream=stream,
                content=accepted,
                sequence=self._sequence,
                received_at=time.time(),
            )
            if self.on_chunk is not None and accepted:
                self.on_chunk(chunk)
            return self.state()

    def state(self) -> OutputBudgetState:
        with self._lock:
            return OutputBudgetState(
                stdout_bytes=self._stdout_seen,
                stderr_bytes=self._stderr_seen,
                combined_bytes=self._stdout_seen + self._stderr_seen,
                stdout_truncated=self._stdout_truncated,
                stderr_truncated=self._stderr_truncated,
                combined_truncated=self._combined_truncated,
            )

    def output(self) -> ProcessOutput:
        with self._lock:
            return ProcessOutput(
                stdout=bytes(self._stdout),
                stderr=bytes(self._stderr),
                stdout_truncated=self._stdout_truncated,
                stderr_truncated=self._stderr_truncated,
                combined_truncated=self._combined_truncated,
            )


class ProcessStreamPump:
    """Reads stdout/stderr concurrently to avoid pipe deadlock."""

    def __init__(
        self,
        stdout: BinaryIO,
        stderr: BinaryIO,
        collector: OutputBudgetCollector,
        *,
        chunk_size: int = 64 * 1024,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.collector = collector
        self.chunk_size = int(chunk_size)
        self._queue: queue.Queue[tuple[str, bytes] | tuple[str, None]] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._closed = {"stdout": False, "stderr": False}

    def start(self) -> None:
        for name, stream in (("stdout", self.stdout), ("stderr", self.stderr)):
            thread = threading.Thread(
                target=self._read,
                args=(name, stream),
                name=f"gateway-{name}-reader",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def drain(
        self,
        *,
        timeout_seconds: float = 0.05,
    ) -> OutputBudgetState:
        while True:
            try:
                stream, content = self._queue.get(timeout=timeout_seconds)
            except queue.Empty:
                break
            if content is None:
                self._closed[stream] = True
            else:
                self.collector.append(stream, content)
        return self.collector.state()

    def finish(self, *, timeout_seconds: float = 2.0) -> ProcessOutput:
        deadline = time.monotonic() + timeout_seconds
        while not all(self._closed.values()) and time.monotonic() < deadline:
            self.drain(timeout_seconds=0.05)
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.drain(timeout_seconds=0.0)
        return self.collector.output()

    def _read(self, name: str, stream: BinaryIO) -> None:
        try:
            while True:
                content = stream.read(self.chunk_size)
                if not content:
                    break
                self._queue.put((name, bytes(content)))
        finally:
            self._queue.put((name, None))


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""
        self._lock = threading.RLock()

    def cancel(self, reason: str = "cancelled") -> bool:
        with self._lock:
            first = not self._event.is_set()
            if first:
                self._reason = str(reason)
                self._event.set()
            return first

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class ProcessBudgetRegistry:
    def __init__(self) -> None:
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = threading.RLock()

    def register(self, command_id: str) -> CancellationToken:
        with self._lock:
            if command_id in self._tokens:
                raise ValueError(f"command budget already registered: {command_id}")
            token = CancellationToken()
            self._tokens[command_id] = token
            return token

    def cancel(self, command_id: str, reason: str) -> bool:
        with self._lock:
            token = self._tokens.get(command_id)
        return token.cancel(reason) if token is not None else False

    def release(self, command_id: str) -> None:
        with self._lock:
            self._tokens.pop(command_id, None)

    def descriptor(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "active_commands": sorted(self._tokens),
                "cancelled_commands": sorted(
                    key for key, value in self._tokens.items() if value.cancelled
                ),
            }
