from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.zyra_api import deployment_api, main as api
from zyra_orchestration.deployment import DeploymentOrchestrator
from zyra_orchestration.deployment import node_server
from zyra_orchestration.deployment.models import digest


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


def test_node_server_preserves_receipt_digest_and_redacts_diagnostics() -> None:
    result = {
        "output": {
            "nested": {
                "signature": "receipt-evidence-signature",
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }
        }
    }
    receipt = {
        "schema": "zyra.deployment-node-receipt/v1",
        "result": result,
        "result_digest": digest(result),
    }

    outbound = node_server._outbound_payload(receipt)

    assert outbound == receipt
    assert digest(outbound["result"]) == outbound["result_digest"]
    diagnostic = node_server._outbound_payload(
        {
            "schema": "zyra.deployment-error/v1",
            "details": {"authorization": "Bearer synthetic-test-value"},
        }
    )
    assert diagnostic["details"]["authorization"] == "<redacted>"


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


def test_one_shot_lifecycle_manager_does_not_fence_nodes_to_cli_process(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    orchestrator = DeploymentOrchestrator(
        project,
        state_root=project / "deployment-state",
        environment={},
        fence_nodes_to_supervisor=False,
    )

    environment = orchestrator.processes._base_environment()

    assert "ZYRA_DEPLOYMENT_SUPERVISOR_PID" not in environment
    assert "ZYRA_DEPLOYMENT_SUPERVISOR_CREATE_TIME" not in environment
    assert environment["ZYRA_DEPLOYMENT_SUPERVISED"] == "1"


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


def test_deployment_node_watchdog_is_optional_only_when_identity_is_absent(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ZYRA_DEPLOYMENT_SUPERVISOR_PID", raising=False)
    monkeypatch.delenv(
        "ZYRA_DEPLOYMENT_SUPERVISOR_CREATE_TIME",
        raising=False,
    )

    assert node_server._start_supervisor_watchdog(object()) is None

    monkeypatch.setenv("ZYRA_DEPLOYMENT_SUPERVISOR_PID", "123")
    with pytest.raises(SystemExit, match="identity is incomplete"):
        node_server._start_supervisor_watchdog(object())


def test_provider_env_loader_reads_only_the_exact_allowlisted_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(api, "_FILE_MANAGED_PROVIDER_ENV", set())
    for key, filename, provider, _model in api._PROVIDER_ENV_FILES:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(api.PROVIDER_ENABLED_ENV[provider], raising=False)
        (tmp_path / filename).write_text(
            f"IGNORED_SECRET=must-not-load\n{key}='{key}-value'\n",
            encoding="utf-8",
        )
    monkeypatch.delenv("IGNORED_SECRET", raising=False)

    configured = api._load_configured_provider_environment()

    assert configured == tuple(item[0] for item in api._PROVIDER_ENV_FILES)
    assert os.environ.get("IGNORED_SECRET") is None
    assert api._preferred_configured_provider() == (
        "deepseek",
        "deepseek-v4-flash",
    )

    (tmp_path / ".env.deepseek.local").write_text("", encoding="utf-8")
    configured_after_removal = api._load_configured_provider_environment()
    assert "DEEPSEEK_API_KEY" not in configured_after_removal
    assert os.environ.get("DEEPSEEK_API_KEY") is None
    assert api._preferred_configured_provider() == ("zhipu", "glm-5.2")


def test_provider_env_loader_disable_flag_removes_file_managed_values_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(api, "_FILE_MANAGED_PROVIDER_ENV", set())
    for key, filename, provider, _model in api._PROVIDER_ENV_FILES:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(api.PROVIDER_ENABLED_ENV[provider], raising=False)
        (tmp_path / filename).write_text(
            f"{key}={key}-file-value\n",
            encoding="utf-8",
        )

    assert api._load_configured_provider_environment() == tuple(
        item[0] for item in api._PROVIDER_ENV_FILES
    )
    monkeypatch.setenv("ZYRA_DISABLE_LOCAL_PROVIDER_ENV_FILES", "1")

    assert api._load_configured_provider_environment() == ()
    assert all(
        os.environ.get(key) is None
        for key, _filename, _provider, _model in api._PROVIDER_ENV_FILES
    )
    assert api._preferred_configured_provider() is None

    # The guard disables implicit files, not a credential that the caller
    # deliberately supplies in its process environment.
    monkeypatch.setenv("ZAI_API_KEY", "explicit-process-value")
    assert api._load_configured_provider_environment() == ("ZAI_API_KEY",)
    assert api._preferred_configured_provider() == ("zhipu", "glm-5.2")


def test_provider_switches_retain_credentials_but_admit_only_deepseek(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(api, "_FILE_MANAGED_PROVIDER_ENV", set())
    enabled = {
        "deepseek": "true",
        "zhipu": "false",
        "kimi-platform": "false",
    }
    for key, filename, provider, _model in api._PROVIDER_ENV_FILES:
        enabled_name = api.PROVIDER_ENABLED_ENV[provider]
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(enabled_name, raising=False)
        (tmp_path / filename).write_text(
            f"{key}={key}-value\n{enabled_name}={enabled[provider]}\n",
            encoding="utf-8",
        )

    configured = api._load_configured_provider_environment()

    assert configured == ("DEEPSEEK_API_KEY",)
    assert all(
        os.environ.get(key) == f"{key}-value"
        for key, _filename, _provider, _model in api._PROVIDER_ENV_FILES
    )
    assert api._preferred_configured_provider() == (
        "deepseek",
        "deepseek-v4-flash",
    )
