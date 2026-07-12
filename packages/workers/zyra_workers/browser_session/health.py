from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .models import BrowserSessionDiagnostic, BrowserSessionRef


@dataclass(frozen=True, slots=True)
class BrowserHealthCheck:
    name: str
    ok: bool
    required: bool
    summary: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BrowserHealthReport:
    ok: bool
    checks: tuple[BrowserHealthCheck, ...]
    diagnostic: BrowserSessionDiagnostic | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
            "diagnostic": self.diagnostic.to_dict() if self.diagnostic else None,
        }


class BrowserHealthRuntime:
    def __init__(self, *, state_root: Path, runtime_root: Path, artifact_root: Path) -> None:
        self.state_root = state_root
        self.runtime_root = runtime_root
        self.artifact_root = artifact_root

    def roots(self) -> tuple[BrowserHealthCheck, ...]:
        checks: list[BrowserHealthCheck] = []
        for name, root in (
            ("state_root", self.state_root),
            ("runtime_root", self.runtime_root),
            ("artifact_root", self.artifact_root),
        ):
            exists = root.exists()
            directory = exists and root.is_dir()
            writable = False
            if directory:
                probe = root / ".browser-health-probe"
                try:
                    probe.write_text("ok", encoding="utf-8")
                    probe.unlink()
                    writable = True
                except OSError:
                    writable = False
            checks.append(
                BrowserHealthCheck(
                    name=name,
                    ok=directory and writable,
                    required=True,
                    summary="available" if directory and writable else "missing or not writable",
                    metadata={"path": str(root), "exists": exists, "directory": directory, "writable": writable},
                )
            )
        return tuple(checks)

    def session(
        self,
        session: BrowserSessionRef,
        *,
        process_alive: bool,
        cdp_connected: bool,
        target_count: int,
        event_bus_generation: int,
        profile_healthy: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserHealthReport:
        checks = list(self.roots())
        running = session.status in {"running", "reconnecting"}
        checks.extend(
            [
                BrowserHealthCheck("session_state", bool(session.status), True, session.status),
                BrowserHealthCheck("profile", profile_healthy, True, "healthy" if profile_healthy else "unhealthy"),
                BrowserHealthCheck(
                    "process",
                    process_alive or session.process_id is None,
                    session.process_id is not None,
                    "alive" if process_alive else "not managed or stopped",
                ),
                BrowserHealthCheck(
                    "cdp_connection",
                    cdp_connected or not running,
                    running,
                    "connected" if cdp_connected else "disconnected",
                ),
                BrowserHealthCheck(
                    "focus",
                    bool(session.active_target_id) or not running,
                    running,
                    session.active_target_id or "no active target",
                ),
            ]
        )
        issues = tuple(check.summary for check in checks if check.required and not check.ok)
        diagnostic = BrowserSessionDiagnostic(
            ok=not issues,
            session_id=session.session_id,
            status=session.status,
            process_alive=process_alive,
            cdp_connected=cdp_connected,
            target_count=target_count,
            active_target_id=session.active_target_id,
            event_bus_generation=event_bus_generation,
            profile_healthy=profile_healthy,
            issues=issues,
            metadata=dict(metadata or {}),
        )
        return BrowserHealthReport(
            ok=not any(check.required and not check.ok for check in checks),
            checks=tuple(checks),
            diagnostic=diagnostic,
        )
