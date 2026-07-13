from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .blank_runtime import AboutBlankRuntime, BlankState
from .download_runtime import DownloadRisk, DownloadState
from .models import (
    ATTACHED_WATCHDOGS,
    BrowserObservation,
    HealthStatus,
    Severity,
    SignalKind,
    WatchdogMaturity,
    WatchdogName,
    WatchdogSignal,
)
from .permission_runtime import PermissionDrift
from .security_policy import SecurityDecision, SecurityVerdict


class AttachedWatchdog(Protocol):
    name: WatchdogName
    maturity: WatchdogMaturity

    def evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        ...

    def snapshot(self) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class WatchdogPolicy:
    emit_healthy_transitions: bool = False
    dedupe_window_ms: int = 5_000
    screenshot_required_after_terminal_action: bool = True
    about_blank_grace_ms: int = 10_000
    storage_persist_grace_ms: int = 5_000
    maximum_popups: int = 8

    def __post_init__(self) -> None:
        if min(
            self.dedupe_window_ms,
            self.about_blank_grace_ms,
            self.storage_persist_grace_ms,
            self.maximum_popups,
        ) < 0:
            raise ValueError("watchdog policy values must be non-negative")


@dataclass(slots=True)
class WatchdogState:
    evaluations: int = 0
    emitted: int = 0
    last_sequence: int = 0
    last_status: HealthStatus = HealthStatus.UNKNOWN
    last_signal_at_ms: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluations": self.evaluations,
            "emitted": self.emitted,
            "last_sequence": self.last_sequence,
            "last_status": str(self.last_status),
            "metadata": dict(self.metadata),
        }


class BaseAttachedWatchdog:
    name: WatchdogName
    maturity = WatchdogMaturity.ACTIVE

    def __init__(
        self,
        *,
        policy: WatchdogPolicy | None = None,
    ) -> None:
        self.policy = policy or WatchdogPolicy()
        self.state = WatchdogState()

    def evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        if observation.sequence < self.state.last_sequence:
            raise RuntimeError(
                f"{self.name} received an out-of-order browser observation"
            )
        self.state.evaluations += 1
        self.state.last_sequence = observation.sequence
        signals = self._evaluate(observation)
        output: list[WatchdogSignal] = []
        for signal in signals:
            if signal.scope != observation.scope:
                raise RuntimeError(f"{self.name} emitted a foreign-scope signal")
            if self._deduplicated(signal, observation.monotonic_ms):
                continue
            output.append(signal)
            self.state.emitted += 1
            self.state.last_status = signal.status
        return tuple(output)

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        raise NotImplementedError

    def _signal(
        self,
        observation: BrowserObservation,
        kind: SignalKind,
        status: HealthStatus,
        severity: Severity,
        summary: str,
        *,
        retryable: bool = False,
        terminal: bool = False,
        artifact_ids: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> WatchdogSignal:
        return WatchdogSignal(
            scope=observation.scope,
            watchdog=self.name,
            kind=kind,
            status=status,
            severity=severity,
            summary=summary,
            sequence=observation.sequence,
            retryable=retryable,
            terminal=terminal,
            artifact_ids=tuple(artifact_ids),
            metadata=dict(metadata or {}),
        )

    def _deduplicated(
        self,
        signal: WatchdogSignal,
        at_ms: int,
    ) -> bool:
        key = f"{signal.kind}:{signal.status}:{signal.summary}"
        previous = self.state.last_signal_at_ms.get(key)
        self.state.last_signal_at_ms[key] = at_ms
        return (
            previous is not None
            and at_ms - previous < self.policy.dedupe_window_ms
        )

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "name": str(self.name),
            "maturity": str(self.maturity),
            **self.state.to_dict(),
        }


class LocalBrowserWatchdog(BaseAttachedWatchdog):
    name = WatchdogName.LOCAL_BROWSER

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        if observation.process_running is False and not observation.intentional_stop:
            return (
                self._signal(
                    observation,
                    SignalKind.PROCESS_EXITED,
                    HealthStatus.TERMINATED,
                    Severity.CRITICAL,
                    "Browser process is not running.",
                    retryable=True,
                    terminal=True,
                    metadata={
                        "process_id": observation.process_id,
                        "exit_code": observation.process_exit_code,
                    },
                ),
            )
        if (
            observation.intentional_stop
            and observation.process_running is True
        ):
            return (
                self._signal(
                    observation,
                    SignalKind.PROCESS_LEAK,
                    HealthStatus.DEGRADED,
                    Severity.WARNING,
                    "Browser process remained alive after intentional stop.",
                    retryable=True,
                    metadata={"process_id": observation.process_id},
                ),
            )
        if (
            self.policy.emit_healthy_transitions
            and observation.process_running is True
        ):
            return (
                self._signal(
                    observation,
                    SignalKind.PROCESS_STARTED,
                    HealthStatus.HEALTHY,
                    Severity.INFO,
                    "Browser process is running.",
                    metadata={"process_id": observation.process_id},
                ),
            )
        return ()


class SecurityWatchdog(BaseAttachedWatchdog):
    name = WatchdogName.SECURITY

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        raw = observation.metadata.get("security_verdict")
        if not isinstance(raw, Mapping):
            return ()
        decision = str(raw.get("decision") or "")
        if decision == str(SecurityDecision.DENY):
            return (
                self._signal(
                    observation,
                    SignalKind.SECURITY_BLOCK,
                    HealthStatus.UNHEALTHY,
                    Severity.ERROR,
                    str(raw.get("summary") or "Browser request was blocked by security policy."),
                    retryable=False,
                    terminal=True,
                    metadata=dict(raw),
                ),
            )
        if decision == str(SecurityDecision.AUDIT):
            return (
                self._signal(
                    observation,
                    SignalKind.SECURITY_DEGRADED,
                    HealthStatus.DEGRADED,
                    Severity.WARNING,
                    str(raw.get("summary") or "Browser request requires a security audit."),
                    metadata=dict(raw),
                ),
            )
        return ()


class DownloadsWatchdog(BaseAttachedWatchdog):
    name = WatchdogName.DOWNLOADS

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        signals: list[WatchdogSignal] = []
        for raw in observation.download_items:
            state = str(raw.get("state") or "")
            download_id = str(raw.get("download_id") or "")
            risks = tuple(str(item) for item in raw.get("risks") or ())
            if state == str(DownloadState.FAILED):
                signals.append(
                    self._signal(
                        observation,
                        SignalKind.DOWNLOAD_FAILED,
                        HealthStatus.UNHEALTHY,
                        Severity.ERROR,
                        "Browser download failed.",
                        retryable=True,
                        terminal=True,
                        metadata={
                            "download_id": download_id,
                            "error": str(raw.get("error") or ""),
                            "risks": list(risks),
                        },
                    )
                )
            elif state == str(DownloadState.QUARANTINED):
                signals.append(
                    self._signal(
                        observation,
                        SignalKind.DOWNLOAD_QUARANTINED,
                        HealthStatus.DEGRADED,
                        Severity.WARNING,
                        "Browser download was quarantined.",
                        retryable=False,
                        terminal=True,
                        artifact_ids=(
                            (str(raw.get("artifact_id")),)
                            if raw.get("artifact_id")
                            else ()
                        ),
                        metadata={
                            "download_id": download_id,
                            "risks": list(risks),
                        },
                    )
                )
            elif state in {
                str(DownloadState.COMPLETE),
                str(DownloadState.PUBLISHED),
            } and self.policy.emit_healthy_transitions:
                signals.append(
                    self._signal(
                        observation,
                        SignalKind.DOWNLOAD_COMPLETED,
                        HealthStatus.HEALTHY,
                        Severity.INFO,
                        "Browser download completed.",
                        artifact_ids=(
                            (str(raw.get("artifact_id")),)
                            if raw.get("artifact_id")
                            else ()
                        ),
                        metadata={"download_id": download_id},
                    )
                )
        return tuple(signals)


class StorageStateWatchdog(BaseAttachedWatchdog):
    name = WatchdogName.STORAGE_STATE

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        if not observation.storage_dirty:
            return ()
        if observation.storage_persisted_at:
            if self.policy.emit_healthy_transitions:
                return (
                    self._signal(
                        observation,
                        SignalKind.STORAGE_PERSISTED,
                        HealthStatus.HEALTHY,
                        Severity.INFO,
                        "Dirty browser storage state was persisted.",
                        metadata={
                            "persisted_at": observation.storage_persisted_at,
                        },
                    ),
                )
            return ()
        dirty_since = int(
            observation.metadata.get("storage_dirty_since_ms")
            or observation.monotonic_ms
        )
        elapsed = max(0, observation.monotonic_ms - dirty_since)
        if elapsed >= self.policy.storage_persist_grace_ms:
            return (
                self._signal(
                    observation,
                    SignalKind.STORAGE_FAILED,
                    HealthStatus.UNHEALTHY,
                    Severity.ERROR,
                    "Dirty browser storage state missed its persistence deadline.",
                    retryable=True,
                    terminal=True,
                    metadata={
                        "dirty_since_ms": dirty_since,
                        "elapsed_ms": elapsed,
                        "grace_ms": self.policy.storage_persist_grace_ms,
                    },
                ),
            )
        return (
            self._signal(
                observation,
                SignalKind.STORAGE_DIRTY,
                HealthStatus.DEGRADED,
                Severity.WARNING,
                "Browser storage state is dirty and awaiting persistence.",
                metadata={
                    "dirty_since_ms": dirty_since,
                    "elapsed_ms": elapsed,
                },
            ),
        )


class PermissionsWatchdog(BaseAttachedWatchdog):
    name = WatchdogName.PERMISSIONS

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        expected = set(observation.permissions_expected)
        reported = set(observation.permissions_reported)
        missing = sorted(expected - reported)
        unexpected = sorted(reported - expected)
        if missing or unexpected:
            return (
                self._signal(
                    observation,
                    SignalKind.PERMISSION_DRIFT,
                    HealthStatus.UNHEALTHY,
                    Severity.ERROR,
                    "Browser permission state drifted from the 03A-bound expectation.",
                    retryable=False,
                    terminal=True,
                    metadata={
                        "expected": sorted(expected),
                        "reported": sorted(reported),
                        "missing": missing,
                        "unexpected": unexpected,
                        "decision_owner": "M1-03A",
                    },
                ),
            )
        return ()


class ScreenshotWatchdog(BaseAttachedWatchdog):
    name = WatchdogName.SCREENSHOT

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        if (
            observation.action_terminal
            and self.policy.screenshot_required_after_terminal_action
            and not observation.screenshot_artifact_id
        ):
            return (
                self._signal(
                    observation,
                    SignalKind.SCREENSHOT_MISSING,
                    HealthStatus.DEGRADED,
                    Severity.WARNING,
                    "Terminal browser action has no screenshot evidence.",
                    retryable=True,
                    terminal=False,
                    metadata={
                        "action_ok": observation.action_ok,
                        "action_error": observation.action_error,
                    },
                ),
            )
        if observation.screenshot_artifact_id and self.policy.emit_healthy_transitions:
            return (
                self._signal(
                    observation,
                    SignalKind.SCREENSHOT_CAPTURED,
                    HealthStatus.HEALTHY,
                    Severity.INFO,
                    "Browser screenshot evidence is attached.",
                    artifact_ids=(observation.screenshot_artifact_id,),
                ),
            )
        return ()


class PopupsWatchdog(BaseAttachedWatchdog):
    name = WatchdogName.POPUPS

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        popup_count = len(observation.popup_target_ids)
        if popup_count > self.policy.maximum_popups:
            return (
                self._signal(
                    observation,
                    SignalKind.POPUP_QUARANTINED,
                    HealthStatus.DEGRADED,
                    Severity.WARNING,
                    "Browser popup count exceeded the attached policy.",
                    retryable=False,
                    metadata={
                        "popup_count": popup_count,
                        "maximum_popups": self.policy.maximum_popups,
                        "popup_target_ids": list(observation.popup_target_ids),
                    },
                ),
            )
        if popup_count and self.policy.emit_healthy_transitions:
            return (
                self._signal(
                    observation,
                    SignalKind.POPUP_OPENED,
                    HealthStatus.HEALTHY,
                    Severity.INFO,
                    "Browser popup targets are tracked.",
                    metadata={
                        "popup_count": popup_count,
                        "popup_target_ids": list(observation.popup_target_ids),
                    },
                ),
            )
        return ()


class AboutBlankWatchdog(BaseAttachedWatchdog):
    name = WatchdogName.ABOUT_BLANK

    def _evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        normalized = observation.current_url.strip().casefold()
        blank = normalized in {
            "",
            "about:blank",
            "about:newtab",
            "chrome://newtab/",
            "edge://newtab/",
        }
        if not blank:
            return ()
        blank_since = int(
            observation.metadata.get("about_blank_since_ms")
            or observation.monotonic_ms
        )
        elapsed = max(0, observation.monotonic_ms - blank_since)
        expected = bool(observation.metadata.get("about_blank_expected"))
        if expected and elapsed < self.policy.about_blank_grace_ms:
            if self.policy.emit_healthy_transitions:
                return (
                    self._signal(
                        observation,
                        SignalKind.ABOUT_BLANK_EXPECTED,
                        HealthStatus.HEALTHY,
                        Severity.INFO,
                        "about:blank is within its expected navigation grace.",
                        metadata={
                            "elapsed_ms": elapsed,
                            "grace_ms": self.policy.about_blank_grace_ms,
                        },
                    ),
                )
            return ()
        return (
            self._signal(
                observation,
                SignalKind.ABOUT_BLANK_STALLED,
                HealthStatus.DEGRADED,
                Severity.WARNING,
                "Browser target remained on about:blank beyond its grace.",
                retryable=True,
                metadata={
                    "elapsed_ms": elapsed,
                    "grace_ms": self.policy.about_blank_grace_ms,
                    "expected": expected,
                },
            ),
        )


class BrowserWatchdogRegistry:
    """Fixed attached-watchdog lifecycle for the browser main path."""

    def __init__(
        self,
        watchdogs: Sequence[AttachedWatchdog] | None = None,
        *,
        policy: WatchdogPolicy | None = None,
    ) -> None:
        shared = policy or WatchdogPolicy()
        values = tuple(
            watchdogs
            or (
                LocalBrowserWatchdog(policy=shared),
                SecurityWatchdog(policy=shared),
                DownloadsWatchdog(policy=shared),
                StorageStateWatchdog(policy=shared),
                PermissionsWatchdog(policy=shared),
                ScreenshotWatchdog(policy=shared),
                PopupsWatchdog(policy=shared),
                AboutBlankWatchdog(policy=shared),
            )
        )
        names = tuple(item.name for item in values)
        expected = tuple(
            item
            for item in ATTACHED_WATCHDOGS
            if item != WatchdogName.CRASH_DETECTOR
        )
        if set(names) != set(expected):
            raise ValueError(
                "attached watchdog registry must contain exactly the active "
                f"non-crash set; got {sorted(str(item) for item in names)}"
            )
        if len(names) != len(set(names)):
            raise ValueError("attached watchdog registry has duplicate names")
        if any(item.maturity != WatchdogMaturity.ACTIVE for item in values):
            raise ValueError("attached watchdogs must be active")
        self._watchdogs = {
            item.name: item
            for item in values
        }
        self._evaluation_count = 0

    def evaluate(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        signals: list[WatchdogSignal] = []
        for name in ATTACHED_WATCHDOGS:
            if name == WatchdogName.CRASH_DETECTOR:
                continue
            watchdog = self._watchdogs[name]
            signals.extend(watchdog.evaluate(observation))
        self._evaluation_count += 1
        return tuple(signals)

    def require(
        self,
        name: WatchdogName,
    ) -> AttachedWatchdog:
        value = self._watchdogs.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.watchdog-registry.v1",
            "attached": [str(item) for item in ATTACHED_WATCHDOGS],
            "active": [
                str(item.name)
                for item in self._watchdogs.values()
            ],
            "crash_detector": {
                "name": str(WatchdogName.CRASH_DETECTOR),
                "active": True,
                "implementation": "BrowserCrashDetector",
                "upstream_crash_watchdog_attached": False,
            },
            "evaluations": self._evaluation_count,
            "watchdogs": {
                str(name): dict(watchdog.snapshot())
                for name, watchdog in self._watchdogs.items()
            },
        }
