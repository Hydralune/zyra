from __future__ import annotations

from zyra_orchestration.topology_policy.production import (
    Phase2StrongestProductionBridge,
)


def test_checkpointed_side_effect_recovery_requires_complete_durable_binding(
    monkeypatch,
) -> None:
    reconciliation = {"side_effect_confirmed": True}

    monkeypatch.setenv("ZYRA_BENCHMARK_LONG_HORIZON", "true")
    monkeypatch.setenv("ZYRA_BENCHMARK_DOCKER_CONTAINER", "container-1")
    monkeypatch.setenv("ZYRA_BENCHMARK_DOCKER_WORKDIR", "/app/task")

    assert (
        Phase2StrongestProductionBridge
        ._checkpointed_side_effect_recovery_allowed(reconciliation)
        is True
    )

    monkeypatch.delenv("ZYRA_BENCHMARK_DOCKER_WORKDIR")
    assert (
        Phase2StrongestProductionBridge
        ._checkpointed_side_effect_recovery_allowed(reconciliation)
        is False
    )


def test_checkpointed_side_effect_recovery_stays_fail_closed_by_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ZYRA_BENCHMARK_LONG_HORIZON", raising=False)
    monkeypatch.delenv("ZYRA_BENCHMARK_DOCKER_CONTAINER", raising=False)
    monkeypatch.delenv("ZYRA_BENCHMARK_DOCKER_WORKDIR", raising=False)

    assert (
        Phase2StrongestProductionBridge
        ._checkpointed_side_effect_recovery_allowed(
            {"side_effect_confirmed": True}
        )
        is False
    )
    assert (
        Phase2StrongestProductionBridge
        ._checkpointed_side_effect_recovery_allowed(
            {"side_effect_confirmed": False}
        )
        is False
    )
