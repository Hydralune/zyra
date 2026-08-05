from __future__ import annotations

from types import SimpleNamespace

from apps.api.zyra_api import deployment_api


def test_reset_deployment_api_stops_owned_processes(monkeypatch) -> None:
    stopped: list[bool] = []
    facade = SimpleNamespace(
        orchestrator=SimpleNamespace(
            processes=SimpleNamespace(
                stop_all=lambda: stopped.append(True),
            )
        )
    )
    monkeypatch.setattr(deployment_api, "_API", facade)
    monkeypatch.setattr(deployment_api, "_KEY", "candidate")

    deployment_api.reset_deployment_api()

    assert stopped == [True]
    assert deployment_api._API is None
    assert deployment_api._KEY == ""
