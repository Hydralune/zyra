from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - adoption fails closed without the existing runtime dependency.
    psutil = None  # type: ignore[assignment]

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


class ChromeSandboxBypassRequired(RuntimeError):
    code = "browser_unsafe_sandbox_bypass_required"


@dataclass(frozen=True, slots=True)
class ChromeLaunchPolicy:
    headless: bool = True
    keep_alive: bool = False
    startup_timeout_seconds: float = 20.0
    shutdown_timeout_seconds: float = 5.0
    allow_unsafe_sandbox_bypass: bool = False
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
    session_id: str = ""

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
            switches.extend(("--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio"))
            if os.name == "nt" and self.policy.allow_unsafe_sandbox_bypass:
                switches.extend(("--disable-gpu-sandbox", "--no-sandbox"))
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
        process: Any,
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

    @property
    def executable_path(self) -> Path | None:
        if not self.command:
            return None
        return Path(self.command[0])

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
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            if process.poll() is not None:
                with self._lock:
                    self._state = ChromeProcessState.EXITED
                    self._stopped_at = time.monotonic()
                return
            raise
        try:
            process.wait(timeout=self.policy.shutdown_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=self.policy.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired as error:
                with self._lock:
                    self._state = ChromeProcessState.FAILED
                    self._stopped_at = time.monotonic()
                    self._error = "Chrome did not exit after terminate and kill"
                raise RuntimeError(self._error) from error
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

    _pid_custody: dict[int, ChromeProcess] = {}
    _marker_paths: dict[int, Path] = {}
    _pid_custody_lock = threading.RLock()

    def __init__(self, *, custody_root: Path | None = None) -> None:
        # Custody is process-wide so a reconstructed BrowserRuntime can recover
        # the Popen handle for a durable browser-session PID without guessing or
        # issuing an unsafe operating-system kill against an unowned process.
        self._owned = self._pid_custody
        self._markers = self._marker_paths
        self._lock = self._pid_custody_lock
        self._custody_root: Path | None = None
        self._custody_key: bytes | None = None
        if custody_root is not None:
            self.configure_custody(custody_root)

    def configure_custody(self, custody_root: Path) -> None:
        root = Path(custody_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        key_path = root / "chrome-custody.key"
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = key_path.read_bytes()
        else:
            key = secrets.token_bytes(32)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
        if len(key) != 32:
            raise RuntimeError("Chrome custody key is invalid")
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        self._custody_root = root
        self._custody_key = key

    def discover_executable(self) -> Path | None:
        configured = os.environ.get("BROWSER_USE_CHROME_PATH") or os.environ.get("CHROME_PATH")
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured).expanduser())
        for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
            resolved = shutil.which(command)
            if resolved:
                candidates.append(Path(resolved))
        candidates.extend(
            (
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
                Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            )
        )
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        return None

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
            if plan.session_id:
                self._write_custody_marker(plan, handle, debug_port=port)
        except Exception:
            handle.kill()
            raise
        with self._lock:
            if handle.pid is not None:
                self._owned[handle.pid] = handle
        return handle

    def adopt(
        self,
        *,
        session_id: str,
        pid: int,
        endpoint_url: str,
        runtime_dir: Path,
        user_data_dir: Path,
        executable_path: Path | None = None,
        readiness_timeout_seconds: float = 10.0,
    ) -> ChromeProcess:
        marker_path = Path(runtime_dir).expanduser().resolve() / "chrome-custody.json"
        marker = self._read_verified_marker(marker_path)
        expected_endpoint = _normalized_endpoint(endpoint_url)
        expected_user_data = Path(user_data_dir).expanduser().resolve()
        if str(marker.get("session_id") or "") != session_id:
            raise RuntimeError("Chrome custody marker session mismatch")
        if int(marker.get("pid") or 0) != int(pid):
            raise RuntimeError("Chrome custody marker PID mismatch")
        if _normalized_endpoint(str(marker.get("endpoint_url") or "")) != expected_endpoint:
            raise RuntimeError("Chrome custody marker endpoint mismatch")
        if _normalized_path(str(marker.get("user_data_dir") or "")) != _normalized_path(expected_user_data):
            raise RuntimeError("Chrome custody marker profile mismatch")
        process = _verified_process_identity(pid)
        if abs(process.create_time() - float(marker.get("create_time") or 0.0)) > 0.01:
            raise RuntimeError("Chrome custody PID creation time mismatch")
        actual_executable = Path(process.exe()).resolve()
        marker_executable = Path(str(marker.get("executable") or "")).resolve()
        if _normalized_path(actual_executable) != _normalized_path(marker_executable):
            raise RuntimeError("Chrome custody executable mismatch")
        if executable_path is not None and _normalized_path(actual_executable) != _normalized_path(executable_path):
            raise RuntimeError("Chrome custody requested executable mismatch")
        command = tuple(str(item) for item in process.cmdline())
        debug_port = int(marker.get("debug_port") or 0)
        if not _command_has_switch(command, "--remote-debugging-port", str(debug_port)):
            raise RuntimeError("Chrome custody debug port is absent from process command line")
        if not _command_has_switch(command, "--user-data-dir", str(expected_user_data), path_value=True):
            raise RuntimeError("Chrome custody profile is absent from process command line")
        parsed_endpoint = urllib.parse.urlparse(expected_endpoint)
        if int(parsed_endpoint.port or 0) != debug_port:
            raise RuntimeError("Chrome custody endpoint port mismatch")
        policy = ChromeLaunchPolicy(
            headless=bool(marker.get("headless", True)),
            keep_alive=bool(marker.get("keep_alive", True)),
            shutdown_timeout_seconds=float(marker.get("shutdown_timeout_seconds") or 5.0),
            allow_unsafe_sandbox_bypass=bool(marker.get("unsafe_sandbox_bypass", False)),
        )
        handle = ChromeProcess(
            process=_PsutilProcessAdapter(process),
            endpoint=BrowserEndpoint(http_url=expected_endpoint),
            policy=policy,
            command=command,
            owned=True,
        )
        self._wait_ready(handle, timeout=readiness_timeout_seconds)
        with self._lock:
            self._owned[int(pid)] = handle
            self._markers[int(pid)] = marker_path
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
                    self._remove_custody_marker(pid)
        return tuple(removed)

    def process(self, pid: int) -> ChromeProcess | None:
        with self._lock:
            return self._owned.get(int(pid))

    def owns(self, pid: int) -> bool:
        return self.process(pid) is not None

    def executable_path(self, pid: int) -> Path | None:
        handle = self.process(pid)
        return handle.executable_path if handle is not None else None

    def is_alive(self, pid: int) -> bool:
        handle = self.process(pid)
        return bool(handle is not None and handle.poll() is None)

    def snapshot(self, pid: int) -> ChromeProcessSnapshot | None:
        handle = self.process(pid)
        return handle.snapshot() if handle is not None else None

    def stop(self, pid: int, *, force: bool = False) -> ChromeProcessSnapshot | None:
        handle = self.process(pid)
        if handle is None:
            return None
        handle.stop(force=force)
        snapshot = handle.snapshot()
        if handle.poll() is not None:
            with self._lock:
                self._owned.pop(int(pid), None)
            self._remove_custody_marker(int(pid))
        return snapshot

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
                if (
                    os.name == "nt"
                    and handle.policy.headless
                    and not handle.policy.allow_unsafe_sandbox_bypass
                    and int(code) in {2147483651, -2147483645}
                ):
                    raise ChromeSandboxBypassRequired(
                        "Windows Chrome sandbox initialization failed; explicitly set "
                        "browser_allow_unsafe_sandbox_bypass=true only for a trusted isolated lane"
                    )
                raise RuntimeError(f"Chrome exited before CDP readiness with code {code}")
            try:
                BrowserEndpointDiscovery(handle.endpoint, timeout_seconds=min(max(delay * 2, 0.1), 1.0)).discover()
                return
            except DiscoveryError as error:
                last_error = error
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)
        raise TimeoutError(f"Chrome CDP endpoint did not become ready: {type(last_error).__name__ if last_error else 'timeout'}")

    def _write_custody_marker(self, plan: ChromeLaunchPlan, handle: ChromeProcess, *, debug_port: int) -> Path:
        key = self._require_custody_key()
        if handle.pid is None:
            raise RuntimeError("cannot persist Chrome custody without a PID")
        process = _verified_process_identity(handle.pid)
        payload: dict[str, Any] = {
            "schema": "zyra.chrome-process-custody.v1",
            "session_id": plan.session_id,
            "pid": handle.pid,
            "create_time": process.create_time(),
            "executable": str(Path(process.exe()).resolve()),
            "user_data_dir": str(plan.user_data_dir),
            "runtime_dir": str(plan.runtime_dir),
            "debug_host": plan.debug_host,
            "debug_port": debug_port,
            "endpoint_url": handle.endpoint.base_url,
            "headless": plan.policy.headless,
            "keep_alive": plan.policy.keep_alive,
            "shutdown_timeout_seconds": plan.policy.shutdown_timeout_seconds,
            "unsafe_sandbox_bypass": plan.policy.allow_unsafe_sandbox_bypass,
            "nonce": secrets.token_hex(16),
        }
        payload["signature"] = _marker_signature(payload, key)
        marker_path = plan.runtime_dir / "chrome-custody.json"
        temporary = marker_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(marker_path)
        with self._lock:
            self._markers[handle.pid] = marker_path
        return marker_path

    def _read_verified_marker(self, marker_path: Path) -> dict[str, Any]:
        key = self._require_custody_key()
        try:
            value = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Chrome custody marker is unavailable or invalid") from error
        if not isinstance(value, dict) or value.get("schema") != "zyra.chrome-process-custody.v1":
            raise RuntimeError("Chrome custody marker schema is invalid")
        signature = str(value.get("signature") or "")
        if not hmac.compare_digest(signature, _marker_signature(value, key)):
            raise RuntimeError("Chrome custody marker signature mismatch")
        return value

    def _require_custody_key(self) -> bytes:
        if self._custody_key is None:
            raise RuntimeError("Chrome custody is not configured")
        return self._custody_key

    def _remove_custody_marker(self, pid: int) -> None:
        with self._lock:
            marker_path = self._markers.pop(int(pid), None)
        if marker_path is not None:
            try:
                marker_path.unlink(missing_ok=True)
            except OSError:
                pass


class _PsutilProcessAdapter:
    def __init__(self, process: Any) -> None:
        self.process = process
        self.pid = int(process.pid)

    def poll(self) -> int | None:
        try:
            if not self.process.is_running() or self.process.status() == psutil.STATUS_ZOMBIE:
                return 0
        except (psutil.Error, OSError):
            return 0
        return None

    def terminate(self) -> None:
        self.process.terminate()

    def kill(self) -> None:
        self.process.kill()

    def wait(self, timeout: float) -> int:
        try:
            return int(self.process.wait(timeout=timeout) or 0)
        except psutil.TimeoutExpired as error:
            raise subprocess.TimeoutExpired(str(self.pid), timeout) from error


def _reserve_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _verified_process_identity(pid: int) -> Any:
    if psutil is None:
        raise RuntimeError("Chrome custody adoption requires the existing psutil runtime")
    try:
        process = psutil.Process(int(pid))
        process.create_time()
        process.exe()
        process.cmdline()
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            raise RuntimeError("Chrome custody process is not running")
        return process
    except RuntimeError:
        raise
    except (psutil.Error, OSError) as error:
        raise RuntimeError("Chrome custody process identity cannot be verified") from error


def _marker_signature(payload: Mapping[str, Any], key: bytes) -> str:
    unsigned = {str(name): value for name, value in payload.items() if name != "signature"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _normalized_path(value: Path | str) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _normalized_endpoint(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise RuntimeError("Chrome custody endpoint is invalid")
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


def _command_has_switch(command: Sequence[str], name: str, expected: str, *, path_value: bool = False) -> bool:
    prefix = f"{name}="
    for argument in command:
        if argument.startswith(prefix):
            actual = argument[len(prefix):]
            return _normalized_path(actual) == _normalized_path(expected) if path_value else actual == expected
    return False


def _secret_switch(value: str) -> bool:
    normalized = value.casefold()
    return any(token in normalized for token in ("token", "secret", "password", "authorization", "cookie"))
