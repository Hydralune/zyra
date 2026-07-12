from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .discovery import BrowserEndpoint, BrowserEndpointDiscovery, DiscoveryError, RedactedHeaders


class ChromeProcessState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    EXITED = "exited"
    FAILED = "failed"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class ChromeLaunchPolicy:
    headless: bool = True
    keep_alive: bool = False
    startup_timeout_seconds: float = 20.0
    shutdown_timeout_seconds: float = 5.0
    allowed_extra_switches: tuple[str, ...] = (
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-features=Translate",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-first-run",
        "--no-default-browser-check",
    )

    def __post_init__(self) -> None:
        if self.startup_timeout_seconds <= 0 or self.shutdown_timeout_seconds <= 0:
            raise ValueError("Chrome process timeouts must be positive")


@dataclass(frozen=True, slots=True)
class ChromeLaunchPlan:
    executable: Path
    user_data_dir: Path
    downloads_dir: Path
    runtime_dir: Path
    debug_host: str = "127.0.0.1"
    debug_port: int = 0
    policy: ChromeLaunchPolicy = field(default_factory=ChromeLaunchPolicy)
    extra_switches: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "executable", self.executable.expanduser().resolve())
        object.__setattr__(self, "user_data_dir", self.user_data_dir.expanduser().resolve())
        object.__setattr__(self, "downloads_dir", self.downloads_dir.expanduser().resolve())
        object.__setattr__(self, "runtime_dir", self.runtime_dir.expanduser().resolve())
        if not self.executable.is_file():
            raise ValueError(f"Chrome executable does not exist: {self.executable}")
        if self.debug_port < 0 or self.debug_port > 65535:
            raise ValueError("invalid Chrome debug port")
        for switch in self.extra_switches:
            if not switch.startswith("--") or "\n" in switch or "\r" in switch:
                raise ValueError("invalid Chrome switch")

    def command(self, *, assigned_port: int) -> tuple[str, ...]:
        switches = [
            f"--remote-debugging-address={self.debug_host}",
            f"--remote-debugging-port={assigned_port}",
            f"--user-data-dir={self.user_data_dir}",
            "--remote-allow-origins=*",
        ]
        if self.policy.headless:
            switches.extend(("--headless=new", "--hide-scrollbars", "--mute-audio"))
        switches.extend(self.policy.allowed_extra_switches)
        switches.extend(self.extra_switches)
        switches.append("about:blank")
        return (str(self.executable), *tuple(dict.fromkeys(switches)))


@dataclass(frozen=True, slots=True)
class ChromeProcessSnapshot:
    state: ChromeProcessState
    pid: int | None
    endpoint: str
    owned: bool
    keep_alive: bool
    started_at: float | None
    stopped_at: float | None
    exit_code: int | None
    command_switches: tuple[str, ...]
    error: str


class ChromeProcess:
    def __init__(
        self,
        *,
        process: subprocess.Popen[bytes] | None,
        endpoint: BrowserEndpoint,
        policy: ChromeLaunchPolicy,
        command: Sequence[str] = (),
        owned: bool,
    ) -> None:
        self._process = process
        self.endpoint = endpoint
        self.policy = policy
        self.command = tuple(command)
        self.owned = owned
        self._state = ChromeProcessState.RUNNING if owned else ChromeProcessState.EXTERNAL
        self._started_at = time.monotonic()
        self._stopped_at: float | None = None
        self._error = ""
        self._lock = threading.RLock()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def poll(self) -> int | None:
        with self._lock:
            if self._process is None:
                return None
            code = self._process.poll()
            if code is not None and self._state not in {ChromeProcessState.STOPPED, ChromeProcessState.FAILED}:
                self._state = ChromeProcessState.EXITED
                self._stopped_at = time.monotonic()
            return code

    def stop(self, *, force: bool = False) -> None:
        with self._lock:
            if not self.owned or self._process is None:
                return
            if self.policy.keep_alive and not force:
                return
            if self._process.poll() is not None:
                self._state = ChromeProcessState.EXITED
                self._stopped_at = time.monotonic()
                return
            self._state = ChromeProcessState.STOPPING
            process = self._process
        process.terminate()
        try:
            process.wait(timeout=self.policy.shutdown_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.policy.shutdown_timeout_seconds)
        with self._lock:
            self._state = ChromeProcessState.STOPPED
            self._stopped_at = time.monotonic()

    def kill(self) -> None:
        self.stop(force=True)

    def snapshot(self) -> ChromeProcessSnapshot:
        code = self.poll()
        with self._lock:
            switches = tuple(item for item in self.command[1:] if not _secret_switch(item))
            return ChromeProcessSnapshot(
                state=self._state,
                pid=self.pid,
                endpoint=self.endpoint.base_url,
                owned=self.owned,
                keep_alive=self.policy.keep_alive,
                started_at=self._started_at,
                stopped_at=self._stopped_at,
                exit_code=code,
                command_switches=switches,
                error=self._error,
            )


class ChromeProcessController:
    """Owns Chrome launch/attach and readiness, never browser policy decisions."""

    def __init__(self) -> None:
        self._owned: dict[int, ChromeProcess] = {}
        self._lock = threading.RLock()

    def launch(self, plan: ChromeLaunchPlan) -> ChromeProcess:
        for directory in (plan.user_data_dir, plan.downloads_dir, plan.runtime_dir):
            directory.mkdir(parents=True, exist_ok=True)
        port = plan.debug_port or _reserve_port(plan.debug_host)
        command = plan.command(assigned_port=port)
        environment = os.environ.copy()
        environment.update({str(key): str(value) for key, value in plan.environment.items()})
        stdout_path = plan.runtime_dir / "chrome.stdout.log"
        stderr_path = plan.runtime_dir / "chrome.stderr.log"
        creationflags = 0
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open("ab", buffering=0) as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=plan.runtime_dir,
                env=environment,
                creationflags=creationflags,
            )
        endpoint = BrowserEndpoint(http_url=f"http://{plan.debug_host}:{port}")
        handle = ChromeProcess(process=process, endpoint=endpoint, policy=plan.policy, command=command, owned=True)
        try:
            self._wait_ready(handle, timeout=plan.policy.startup_timeout_seconds)
        except Exception:
            handle.kill()
            raise
        with self._lock:
            if handle.pid is not None:
                self._owned[handle.pid] = handle
        return handle

    def attach(
        self,
        endpoint_url: str,
        *,
        headers: Mapping[str, Any] | None = None,
        proxy_url: str = "",
        verify_tls: bool = True,
        readiness_timeout_seconds: float = 10.0,
    ) -> ChromeProcess:
        endpoint = BrowserEndpoint(
            http_url=endpoint_url,
            headers=RedactedHeaders.build(headers),
            proxy_url=proxy_url,
            verify_tls=verify_tls,
        )
        handle = ChromeProcess(
            process=None,
            endpoint=endpoint,
            policy=ChromeLaunchPolicy(keep_alive=True),
            owned=False,
        )
        self._wait_ready(handle, timeout=readiness_timeout_seconds)
        return handle

    def reap(self) -> tuple[ChromeProcessSnapshot, ...]:
        removed: list[ChromeProcessSnapshot] = []
        with self._lock:
            for pid, handle in tuple(self._owned.items()):
                if handle.poll() is not None:
                    removed.append(handle.snapshot())
                    self._owned.pop(pid, None)
        return tuple(removed)

    def stop_all(self, *, force: bool = False) -> tuple[ChromeProcessSnapshot, ...]:
        with self._lock:
            handles = tuple(self._owned.values())
        snapshots: list[ChromeProcessSnapshot] = []
        for handle in handles:
            handle.stop(force=force)
            snapshots.append(handle.snapshot())
        self.reap()
        return tuple(snapshots)

    @staticmethod
    def _wait_ready(handle: ChromeProcess, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        delay = 0.05
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            code = handle.poll()
            if code is not None:
                raise RuntimeError(f"Chrome exited before CDP readiness with code {code}")
            try:
                BrowserEndpointDiscovery(handle.endpoint, timeout_seconds=min(max(delay * 2, 0.1), 1.0)).discover()
                return
            except DiscoveryError as error:
                last_error = error
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)
        raise TimeoutError(f"Chrome CDP endpoint did not become ready: {type(last_error).__name__ if last_error else 'timeout'}")


def _reserve_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _secret_switch(value: str) -> bool:
    normalized = value.casefold()
    return any(token in normalized for token in ("token", "secret", "password", "authorization", "cookie"))
