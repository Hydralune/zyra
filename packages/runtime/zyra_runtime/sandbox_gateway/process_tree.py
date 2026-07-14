from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProcessTreeTermination:
    process_id: int
    graceful_requested: bool
    forced: bool
    return_code: int | None
    elapsed_seconds: float
    error: str = ""

    @property
    def stopped(self) -> bool:
        return self.return_code is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "graceful_requested": self.graceful_requested,
            "forced": self.forced,
            "return_code": self.return_code,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
            "stopped": self.stopped,
        }


class ProcessTreeController:
    def creation_flags(self) -> int:
        if os.name == "nt":
            return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return 0

    def start_new_session(self) -> bool:
        return os.name != "nt"

    def process_group_id(self, process: subprocess.Popen[bytes]) -> int | None:
        if os.name == "nt":
            return process.pid
        try:
            return os.getpgid(process.pid)
        except (OSError, ProcessLookupError):
            return None

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
        reason: str = "",
    ) -> ProcessTreeTermination:
        started = time.monotonic()
        if process.poll() is not None:
            return ProcessTreeTermination(
                process_id=process.pid,
                graceful_requested=False,
                forced=False,
                return_code=process.returncode,
                elapsed_seconds=time.monotonic() - started,
            )
        graceful = False
        forced = False
        error_message = ""
        try:
            graceful = self._graceful(process)
            try:
                process.wait(timeout=max(0.0, grace_seconds))
            except subprocess.TimeoutExpired:
                forced = True
                self._force(process)
                try:
                    process.wait(timeout=max(1.0, grace_seconds))
                except subprocess.TimeoutExpired:
                    error_message = "process tree remained alive after forced termination"
        except Exception as error:
            error_message = f"{type(error).__name__}: {error}"
            try:
                process.kill()
                forced = True
                process.wait(timeout=max(1.0, grace_seconds))
            except Exception:
                pass
        return ProcessTreeTermination(
            process_id=process.pid,
            graceful_requested=graceful,
            forced=forced,
            return_code=process.poll(),
            elapsed_seconds=time.monotonic() - started,
            error=error_message,
        )

    def _graceful(self, process: subprocess.Popen[bytes]) -> bool:
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                process.terminate()
            return True
        try:
            group = os.getpgid(process.pid)
            os.killpg(group, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
        return True

    def _force(self, process: subprocess.Popen[bytes]) -> None:
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            if completed.returncode != 0 and process.poll() is None:
                process.kill()
            return
        try:
            group = os.getpgid(process.pid)
            os.killpg(group, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
