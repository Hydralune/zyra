from __future__ import annotations

import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import digest_value, utc_now


class BrowserProcessState(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    EXITED = "exited"
    TERMINATED = "terminated"
    LEAKED = "leaked"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    browser_session_id: str
    process_id: int
    executable: str
    command_digest: str
    started_at: str
    state: BrowserProcessState
    parent_process_id: int | None = None
    child_process_ids: tuple[int, ...] = ()
    exit_code: int | None = None
    exit_at: str = ""
    intentional_stop: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.process_id <= 0:
            raise ValueError("browser process id must be positive")
        object.__setattr__(
            self,
            "child_process_ids",
            tuple(int(item) for item in self.child_process_ids),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser_session_id": self.browser_session_id,
            "process_id": self.process_id,
            "executable": self.executable,
            "command_digest": self.command_digest,
            "started_at": self.started_at,
            "state": str(self.state),
            "parent_process_id": self.parent_process_id,
            "child_process_ids": list(self.child_process_ids),
            "exit_code": self.exit_code,
            "exit_at": self.exit_at,
            "intentional_stop": self.intentional_stop,
            "metadata": dict(self.metadata),
        }


class LocalBrowserProcessRuntime:
    """Tracks process custody; launch remains the 04A BrowserRuntime owner."""

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._processes: dict[str, ProcessIdentity] = {}

    def register(
        self,
        *,
        browser_session_id: str,
        process_id: int,
        executable: str,
        command: Sequence[str] = (),
        parent_process_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProcessIdentity:
        with self._guard:
            existing = self._processes.get(browser_session_id)
            if existing and existing.process_id != process_id:
                if existing.state not in {
                    BrowserProcessState.EXITED,
                    BrowserProcessState.TERMINATED,
                    BrowserProcessState.RELEASED,
                }:
                    raise RuntimeError("browser session already owns another live process")
            item = ProcessIdentity(
                browser_session_id=browser_session_id,
                process_id=process_id,
                executable=str(Path(executable).resolve(strict=False)),
                command_digest=digest_value(list(command)),
                started_at=utc_now(),
                state=BrowserProcessState.REGISTERED,
                parent_process_id=parent_process_id,
                metadata=dict(metadata or {}),
            )
            self._processes[browser_session_id] = item
            return item

    def running(
        self,
        browser_session_id: str,
        *,
        child_process_ids: Sequence[int] = (),
    ) -> ProcessIdentity:
        with self._guard:
            item = self.require(browser_session_id)
            updated = replace(
                item,
                state=BrowserProcessState.RUNNING,
                child_process_ids=tuple(child_process_ids),
            )
            self._processes[browser_session_id] = updated
            return updated

    def exited(
        self,
        browser_session_id: str,
        *,
        exit_code: int | None,
        intentional: bool,
    ) -> ProcessIdentity:
        with self._guard:
            item = self.require(browser_session_id)
            updated = replace(
                item,
                state=(
                    BrowserProcessState.TERMINATED
                    if intentional
                    else BrowserProcessState.EXITED
                ),
                exit_code=exit_code,
                exit_at=utc_now(),
                intentional_stop=intentional,
            )
            self._processes[browser_session_id] = updated
            return updated

    def audit_cleanup(
        self,
        browser_session_id: str,
        *,
        running_process_ids: Sequence[int],
    ) -> ProcessIdentity:
        with self._guard:
            item = self.require(browser_session_id)
            tracked = {
                item.process_id,
                *item.child_process_ids,
            }
            leaked = tracked & {int(value) for value in running_process_ids}
            if leaked and item.state in {
                BrowserProcessState.EXITED,
                BrowserProcessState.TERMINATED,
                BrowserProcessState.RELEASED,
            }:
                updated = replace(
                    item,
                    state=BrowserProcessState.LEAKED,
                    metadata={
                        **dict(item.metadata),
                        "leaked_process_ids": sorted(leaked),
                    },
                )
                self._processes[browser_session_id] = updated
                return updated
            return item

    def release(
        self,
        browser_session_id: str,
    ) -> ProcessIdentity:
        with self._guard:
            item = self.require(browser_session_id)
            if item.state == BrowserProcessState.LEAKED:
                raise RuntimeError("cannot release browser process custody with live leaks")
            updated = replace(
                item,
                state=BrowserProcessState.RELEASED,
            )
            self._processes[browser_session_id] = updated
            return updated

    def require(
        self,
        browser_session_id: str,
    ) -> ProcessIdentity:
        item = self._processes.get(browser_session_id)
        if item is None:
            raise KeyError(browser_session_id)
        return item

    def projection(
        self,
        *,
        browser_session_id: str = "",
    ) -> dict[str, Any]:
        values = (
            (self.require(browser_session_id),)
            if browser_session_id and browser_session_id in self._processes
            else tuple(self._processes.values())
            if not browser_session_id
            else ()
        )
        return {
            "schema": "zyra.browser-observability.local-process.v1",
            "process_count": len(values),
            "running_count": sum(
                1
                for item in values
                if item.state == BrowserProcessState.RUNNING
            ),
            "unexpected_exit_count": sum(
                1
                for item in values
                if item.state == BrowserProcessState.EXITED
            ),
            "leaked_count": sum(
                1
                for item in values
                if item.state == BrowserProcessState.LEAKED
            ),
            "processes": [item.to_dict() for item in values],
        }
