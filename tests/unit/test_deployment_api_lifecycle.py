from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.zyra_api import deployment_api, main as api
from zyra_orchestration.deployment import DeploymentOrchestrator
from zyra_orchestration.deployment import node_server


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


def test_deployment_state_is_scoped_to_the_active_api_state_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state_root = project / "isolated-state"

    orchestrator = DeploymentOrchestrator(
        project,
        environment={"ZYRA_STATE_ROOT": str(state_root)},
    )

    assert orchestrator.state_root == (state_root / "deployment").resolve()
    assert orchestrator.store.path.parent == orchestrator.state_root
    assert not (project / "tmp" / "deployment").exists()

    environment = orchestrator.processes._base_environment()
    assert environment["ZYRA_DEPLOYMENT_SUPERVISOR_PID"] == str(os.getpid())
    assert float(
        environment["ZYRA_DEPLOYMENT_SUPERVISOR_CREATE_TIME"]
    ) > 0


def test_external_shared_state_root_is_narrowly_scoped_but_explicit_root_is_rejected(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shared_state_root = tmp_path / "isolated-api-state"

    orchestrator = DeploymentOrchestrator(
        project,
        environment={"ZYRA_STATE_ROOT": str(shared_state_root)},
    )

    assert orchestrator.state_root == (shared_state_root / "deployment").resolve()
    assert orchestrator.clean_state.deployment_root == orchestrator.state_root

    with pytest.raises(ValueError, match="must remain inside the project"):
        DeploymentOrchestrator(
            project,
            state_root=tmp_path / "arbitrary-external-deployment-root",
            environment={},
        )


def test_deployment_node_parent_fence_rejects_pid_reuse(monkeypatch) -> None:
    process = SimpleNamespace(
        is_running=lambda: True,
        status=lambda: "running",
        create_time=lambda: 100.0,
    )
    monkeypatch.setattr(node_server.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(node_server.os, "getpid", lambda: 999)

    assert node_server._supervisor_is_alive(123, 100.0) is True
    assert node_server._supervisor_is_alive(123, 101.0) is False
    assert node_server._supervisor_is_alive(999, 100.0) is False


def test_provider_env_loader_reads_only_the_exact_allowlisted_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(api, "_FILE_MANAGED_PROVIDER_ENV", set())
    for key, filename, _provider, _model in api._PROVIDER_ENV_FILES:
        monkeypatch.delenv(key, raising=False)
        (tmp_path / filename).write_text(
            f"IGNORED_SECRET=must-not-load\n{key}='{key}-value'\n",
            encoding="utf-8",
        )
    monkeypatch.delenv("IGNORED_SECRET", raising=False)

    configured = api._load_configured_provider_environment()

    assert configured == tuple(item[0] for item in api._PROVIDER_ENV_FILES)
    assert os.environ.get("IGNORED_SECRET") is None
    assert api._preferred_configured_provider() == ("zhipu", "glm-5.2")

    (tmp_path / ".env.glm.local").write_text("", encoding="utf-8")
    configured_after_removal = api._load_configured_provider_environment()
    assert "ZAI_API_KEY" not in configured_after_removal
    assert os.environ.get("ZAI_API_KEY") is None
    assert api._preferred_configured_provider() == (
        "deepseek",
        "deepseek-v4-flash",
    )
