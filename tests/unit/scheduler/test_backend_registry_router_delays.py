from __future__ import annotations

from types import SimpleNamespace

from zyra_scheduler.backend_registry import router as router_module
from zyra_scheduler.backend_registry.models import BackendDispatchPhase
from zyra_scheduler.backend_registry.router import WorkerDispatchRouter


class _Cancellation:
    def __init__(self) -> None:
        self.check_count = 0

    def throw_if_cancelled(self) -> None:
        self.check_count += 1


def _clock(monkeypatch):
    samples = iter((0.0, 0.005, 0.02))
    sleeps: list[float] = []

    monkeypatch.setattr(router_module.time, "monotonic", lambda: next(samples))

    def record_sleep(seconds: float) -> None:
        assert seconds >= 0
        sleeps.append(seconds)

    monkeypatch.setattr(router_module.time, "sleep", record_sleep)
    return sleeps


def test_interruptible_delay_never_passes_negative_duration(monkeypatch) -> None:
    sleeps = _clock(monkeypatch)
    cancellation = _Cancellation()

    WorkerDispatchRouter._interruptible_delay(0.01, cancellation)

    assert cancellation.check_count == 1
    assert sleeps == [0.005]


def test_session_delay_never_passes_negative_duration(monkeypatch) -> None:
    sleeps = _clock(monkeypatch)
    router = object.__new__(WorkerDispatchRouter)
    router.store = SimpleNamespace(
        get_dispatch_session=lambda _session_id: SimpleNamespace(
            phase=BackendDispatchPhase.RUNNING
        )
    )

    router._session_delay(0.01, "session-delay-boundary")

    assert sleeps == [0.005]
