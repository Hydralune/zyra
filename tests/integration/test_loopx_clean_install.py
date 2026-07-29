from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from zyra_integrations.loopx.install import (
    InstallProfile,
    LoopXDoctor,
    LoopXInstaller,
    LoopXPackageLock,
    LoopXRuntimeError,
)
from zyra_integrations.loopx.runtime import LoopXRuntimeResolver
from zyra_integrations.loopx.runtime.first_task import (
    _loopback_only_network,
    run_first_task_probe,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_clean_workspace_resolves_embedded_runtime_without_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "forbidden-user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    workspace = tmp_path / "workspace"

    first = LoopXRuntimeResolver(PROJECT_ROOT).receipt(workspace)
    second = LoopXRuntimeResolver(PROJECT_ROOT).receipt(workspace)

    assert first == second
    assert first["ready"] is True
    assert first["version"] == "0.2.13"
    assert first["source_kind"] == "embedded_source"
    assert first["archive_extraction"] is False
    assert first["archive_fallback"] is False
    assert Path(first["install_root"]) == (
        PROJECT_ROOT / "packages" / "integrations" / "loopx_runtime"
    ).resolve()
    assert Path(first["state_root"]).is_relative_to(workspace)
    assert not workspace.exists()
    assert not list(user_home.rglob("*"))

    doctor = LoopXDoctor(PROJECT_ROOT).run(
        deep=True,
        workspace_root=workspace,
        python_executable=Path(sys.executable),
    )
    assert doctor["ready"] is True
    canary = next(
        item
        for item in doctor["checks"]
        if item["check"] == "import-and-cli-entry"
    )
    assert canary["details"]["version"] == "0.2.13"
    assert Path(canary["details"]["module_origin"]).is_relative_to(
        Path(first["install_root"])
    )
    assert canary["details"]["pythonpath_override"] is False


def test_historical_install_profiles_converge_on_one_embedded_runtime(
    tmp_path: Path,
) -> None:
    installer = LoopXInstaller(PROJECT_ROOT)
    windows_plan = installer.plan(
        tmp_path / "windows-workspace",
        profile=InstallProfile.WINDOWS_RELEASE,
    )
    linux_plan = installer.plan(
        tmp_path / "linux-workspace",
        profile=InstallProfile.LINUX_WSL,
    )
    assert windows_plan.profile is InstallProfile.EMBEDDED_SOURCE
    assert linux_plan.profile is InstallProfile.EMBEDDED_SOURCE
    assert windows_plan.install_root == linux_plan.install_root
    assert windows_plan.source_digest == linux_plan.source_digest
    assert windows_plan.artifact_name == linux_plan.artifact_name == ""
    assert dict(windows_plan.environment)["ZYRA_LOOPX_RUNTIME_SOURCE"] == "embedded"

    receipt = installer.install(
        tmp_path / "workspace",
        profile=InstallProfile.WINDOWS_RELEASE,
    )
    assert receipt["installer_retired"] is True
    assert receipt["archive_extraction"] is False
    assert receipt["profile"] == "pinned_embedded_source"
    assert not (tmp_path / "workspace").exists()


def test_selected_interpreter_is_probed_and_missing_executable_fails_closed(
    tmp_path: Path,
) -> None:
    installer = LoopXInstaller(PROJECT_ROOT)
    current = installer.check_interpreter(Path(sys.executable))
    assert current["compatible"] is True
    assert current["probed_selected_executable"] is True
    assert Path(current["python"]) == Path(sys.executable).resolve()

    selected = tmp_path / "missing-python.exe"
    with pytest.raises(LoopXRuntimeError) as captured:
        installer.check_interpreter(selected)
    assert captured.value.code == "loopx_python_probe_failed"
    assert captured.value.details["python"] == str(selected.resolve())

    doctor = LoopXDoctor(PROJECT_ROOT).run(
        deep=True,
        python_executable=selected,
    )
    assert doctor["ready"] is False
    import_check = next(
        item
        for item in doctor["checks"]
        if item["check"] == "import-and-cli-entry"
    )
    assert import_check["code"] == "loopx_python_probe_failed"


def test_historical_install_directory_is_preserved_but_ignored(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    retired = workspace / ".zyra" / "loopx" / "install"
    retired.mkdir(parents=True)
    sentinel = retired / "historical-receipt.json"
    sentinel.write_text('{"version":"0.2.4"}\n', encoding="utf-8")

    receipt = LoopXRuntimeResolver(PROJECT_ROOT).receipt(workspace)
    report = LoopXDoctor(PROJECT_ROOT).run(
        deep=True,
        workspace_root=workspace,
    )

    assert receipt["retired_install_root_exists"] is True
    assert Path(receipt["install_root"]) != retired
    assert sentinel.read_text(encoding="utf-8") == '{"version":"0.2.4"}\n'
    state = next(
        item
        for item in report["checks"]
        if item["check"] == "workspace-private-state"
    )
    assert state["details"]["retired_install_root_used"] is False
    assert (
        state["details"]["retired_path_diagnostic"]
        == "historical_install_path_ignored"
    )


def test_package_lock_binds_embedded_source_without_archive_fallback() -> None:
    lock = LoopXPackageLock.load(PROJECT_ROOT)
    assert lock.package["version"] == "0.2.13"
    assert (
        lock.package["source_commit"]
        == "a2c072d412d90839132e1cf39c23dd431c394175"
    )
    assert (
        lock.package["source_tree_commit"]
        == "7232dca45ec2ca996edc43b2d3558edc802c844e"
    )
    assert lock.package["migration_mode"] == "pinned_embedded_source_integration"
    assert lock.artifacts == {}
    assert list(lock.profiles) == ["pinned_embedded_source"]
    serialized = lock.path.read_text(encoding="utf-8").casefold()
    assert "../long-horizon-systems" not in serialized
    assert ".whl" not in serialized
    assert ".tar.gz" not in serialized


def test_first_task_uses_embedded_runtime_without_install_or_home_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "original-loopx-test-home")
    monkeypatch.setenv("USERPROFILE", "original-loopx-test-profile")
    monkeypatch.delenv("ZYRA_NETWORK_MODE", raising=False)
    environment_before = {
        name: os.environ.get(name)
        for name in (
            "HOME",
            "USERPROFILE",
            "ZYRA_NETWORK_MODE",
            "ZYRA_SQLITE_PATH",
            "ZYRA_TOOL_WORKSPACE",
        )
    }
    receipt = run_first_task_probe(
        tmp_path / "detached-first-task",
        package_root=PROJECT_ROOT,
    )

    assert receipt["ready"] is True
    assert receipt["receipt_status"] == "applied"
    assert receipt["lifecycle"] == "enabled"
    assert receipt["sync_cursor"] == 1
    assert receipt["runtime"]["version"] == "0.2.13"
    assert receipt["runtime"]["archive_extraction"] is False
    assert Path(receipt["package_root"]) == PROJECT_ROOT
    assert receipt["retired_install_created"] is False
    assert receipt["user_home_entries"] == []
    assert receipt["claim_is_worker_lease"] is False
    assert receipt["quota_is_execution_budget"] is False
    assert receipt["network_mode"] == "offline"
    assert receipt["network_guard"]["enforced"] is True
    assert receipt["network_guard"]["loopback_connection_count"] >= 2
    assert receipt["network_guard"]["blocked_non_loopback_attempts"] == []
    assert {
        name: os.environ.get(name)
        for name in environment_before
    } == environment_before


def test_first_task_network_guard_blocks_non_loopback_socket() -> None:
    with _loopback_only_network() as receipt:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            with pytest.raises(OSError, match="non-loopback network is blocked"):
                candidate.connect(("203.0.113.1", 443))

    assert receipt["enforced"] is True
    assert receipt["loopback_connection_count"] == 0
    assert receipt["blocked_non_loopback_attempts"] == [
        {
            "operation": "connect",
            "host": "203.0.113.1",
            "port": 443,
        }
    ]
