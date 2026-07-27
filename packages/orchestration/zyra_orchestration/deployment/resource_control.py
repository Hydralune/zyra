from __future__ import annotations

import os
import platform
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psutil

from .models import ResourceEnvelope


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    pid: int
    alive: bool
    cpu_percent: float
    memory_rss_mb: float
    memory_vms_mb: float
    thread_count: int
    open_file_count: int
    child_count: int
    available_system_memory_mb: float
    observed_at_monotonic: float
    limit_supported: bool
    limit_applied: bool
    limit_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "alive": self.alive,
            "cpu_percent": round(self.cpu_percent, 3),
            "memory_rss_mb": round(self.memory_rss_mb, 3),
            "memory_vms_mb": round(self.memory_vms_mb, 3),
            "thread_count": self.thread_count,
            "open_file_count": self.open_file_count,
            "child_count": self.child_count,
            "available_system_memory_mb": round(self.available_system_memory_mb, 3),
            "observed_at_monotonic": self.observed_at_monotonic,
            "limit_supported": self.limit_supported,
            "limit_applied": self.limit_applied,
            "limit_reason": self.limit_reason,
        }


class ResourceController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._applied: dict[int, tuple[ResourceEnvelope, str]] = {}

    def apply(
        self,
        pid: int,
        envelope: ResourceEnvelope,
    ) -> dict[str, Any]:
        process = psutil.Process(pid)
        if not process.is_running():
            raise psutil.NoSuchProcess(pid)
        reasons: list[str] = []
        applied = False
        system = platform.system().casefold()
        try:
            if system == "windows":
                priority = (
                    psutil.BELOW_NORMAL_PRIORITY_CLASS
                    if envelope.cpu_percent <= 50
                    else psutil.NORMAL_PRIORITY_CLASS
                )
                process.nice(priority)
                applied = True
                reasons.append("windows-process-priority")
            else:
                nice = 10 if envelope.cpu_percent <= 35 else 5 if envelope.cpu_percent <= 60 else 0
                process.nice(nice)
                applied = True
                reasons.append("posix-nice")
        except (psutil.AccessDenied, OSError, ValueError) as error:
            reasons.append(f"priority-unavailable:{type(error).__name__}")
        try:
            available_cpus = list(range(psutil.cpu_count(logical=True) or 1))
            desired = max(
                1,
                min(
                    len(available_cpus),
                    round(len(available_cpus) * envelope.cpu_percent / 100),
                ),
            )
            process.cpu_affinity(available_cpus[:desired])
            applied = True
            reasons.append(f"cpu-affinity:{desired}")
        except (AttributeError, psutil.AccessDenied, OSError, ValueError) as error:
            reasons.append(f"cpu-affinity-unavailable:{type(error).__name__}")
        with self._lock:
            self._applied[pid] = (envelope, ",".join(reasons))
        return {
            "schema": "zyra.deployment-resource-control/v1",
            "pid": pid,
            "platform": system,
            "requested": envelope.to_dict(),
            "applied": applied,
            "hard_memory_limit": False,
            "enforcement": reasons,
            "fail_closed_admission": True,
            "note": (
                "OS scheduling controls are applied when supported; memory and "
                "concurrency are additionally enforced by node admission."
            ),
        }

    def observe(self, pid: int) -> ResourceObservation:
        virtual = psutil.virtual_memory()
        with self._lock:
            applied = self._applied.get(pid)
        try:
            process = psutil.Process(pid)
            memory = process.memory_info()
            try:
                open_files = len(process.open_files())
            except (psutil.AccessDenied, OSError):
                open_files = -1
            return ResourceObservation(
                pid=pid,
                alive=process.is_running() and process.status() != psutil.STATUS_ZOMBIE,
                cpu_percent=float(process.cpu_percent(interval=0.0)),
                memory_rss_mb=memory.rss / 1024 / 1024,
                memory_vms_mb=memory.vms / 1024 / 1024,
                thread_count=process.num_threads(),
                open_file_count=open_files,
                child_count=len(process.children(recursive=False)),
                available_system_memory_mb=virtual.available / 1024 / 1024,
                observed_at_monotonic=time.monotonic(),
                limit_supported=applied is not None,
                limit_applied=applied is not None,
                limit_reason=applied[1] if applied else "not-applied",
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return ResourceObservation(
                pid=pid,
                alive=False,
                cpu_percent=0.0,
                memory_rss_mb=0.0,
                memory_vms_mb=0.0,
                thread_count=0,
                open_file_count=0,
                child_count=0,
                available_system_memory_mb=virtual.available / 1024 / 1024,
                observed_at_monotonic=time.monotonic(),
                limit_supported=applied is not None,
                limit_applied=False,
                limit_reason="process-unavailable",
            )

    def admit(
        self,
        pid: int,
        *,
        envelope: ResourceEnvelope,
        requested_memory_mb: int,
        active_dispatches: int,
    ) -> tuple[bool, tuple[str, ...], ResourceObservation]:
        observation = self.observe(pid)
        blockers: list[str] = []
        if not observation.alive:
            blockers.append("process_unavailable")
        if requested_memory_mb > envelope.memory_mb:
            blockers.append("request_memory_exceeds_profile")
        if observation.memory_rss_mb + requested_memory_mb > envelope.memory_mb:
            blockers.append("profile_memory_exhausted")
        if observation.available_system_memory_mb < requested_memory_mb + 64:
            blockers.append("system_memory_pressure")
        if active_dispatches >= envelope.max_concurrency:
            blockers.append("profile_concurrency_exhausted")
        if observation.cpu_percent > max(95.0, envelope.cpu_percent * 1.5):
            blockers.append("profile_cpu_pressure")
        return not blockers, tuple(blockers), observation

    def forget(self, pid: int) -> None:
        with self._lock:
            self._applied.pop(pid, None)

    def applied(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            return {
                pid: {
                    "envelope": envelope.to_dict(),
                    "reason": reason,
                }
                for pid, (envelope, reason) in self._applied.items()
            }


def process_environment_snapshot(
    *,
    allowed_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    allowed = {
        name: bool(str(os.environ.get(name) or "").strip())
        for name in allowed_names
    }
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "pid": os.getpid(),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory_total_mb": round(psutil.virtual_memory().total / 1024 / 1024, 3),
        "credential_presence": allowed,
        "credential_values_exposed": False,
    }


__all__ = [
    "ResourceController",
    "ResourceObservation",
    "process_environment_snapshot",
]
