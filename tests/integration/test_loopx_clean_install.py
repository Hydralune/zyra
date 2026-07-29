from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from zyra_integrations.loopx.install import (
    InstallProfile,
    LoopXDoctor,
    LoopXInstallError,
    LoopXInstaller,
    LoopXPackageLock,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_windows_release_install_is_offline_local_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "forbidden-user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    workspace = tmp_path / "workspace"
    installer = LoopXInstaller(PROJECT_ROOT)

    first = installer.install(
        workspace,
        profile=InstallProfile.WINDOWS_RELEASE,
        python_executable=Path(sys.executable),
    )
    second = installer.install(
        workspace,
        profile=InstallProfile.WINDOWS_RELEASE,
        python_executable=Path(sys.executable),
    )

    assert first["ready"] is True
    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert first["receipt_digest"] == second["receipt_digest"]
    assert Path(first["install_root"]).is_relative_to(workspace)
    assert Path(first["state_root"]).is_relative_to(workspace)
    assert first["network_access"] is False
    assert first["user_home_write"] is False
    assert not list(user_home.rglob("*"))

    doctor = LoopXDoctor(PROJECT_ROOT).run(
        deep=True,
        install_root=Path(first["install_root"]),
        python_executable=Path(sys.executable),
    )
    assert doctor["ready"] is True
    canary = next(
        item
        for item in doctor["checks"]
        if item["check"] == "import-and-cli-entry"
    )
    assert canary["details"]["version"] == "0.2.4"
    assert canary["details"]["user_level_write_count"] == 0


def test_linux_and_windows_profiles_install_the_same_pinned_source(
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
    assert windows_plan.source_digest == linux_plan.source_digest
    assert windows_plan.artifact_name == "wheel"
    assert linux_plan.artifact_name == "source_bundle"
    assert dict(linux_plan.environment)["LOOPX_INSTALL_SKILL"] == "0"
    assert dict(linux_plan.environment)["LOOPX_INSTALL_SLASH_COMMANDS"] == "0"
    assert dict(linux_plan.environment)["LOOPX_INSTALL_CLAUDE"] == "0"

    installed = installer.install(
        tmp_path / "linux-workspace",
        profile=InstallProfile.LINUX_WSL,
        python_executable=Path(sys.executable),
    )
    assert installed["source_digest"] == windows_plan.source_digest
    assert installed["profile"] == InstallProfile.LINUX_WSL.value
    assert (
        Path(installed["install_root"])
        .joinpath(str(installed["module_root"]), "loopx", "__init__.py")
        .is_file()
    )
    doctor = LoopXDoctor(PROJECT_ROOT).run(
        deep=True,
        install_root=Path(installed["install_root"]),
        python_executable=Path(sys.executable),
    )
    assert doctor["ready"] is True


def test_install_fails_closed_for_partial_unwritable_and_missing_dependency(
    tmp_path: Path,
) -> None:
    installer = LoopXInstaller(PROJECT_ROOT)
    partial_workspace = tmp_path / "partial"
    partial_plan = installer.plan(
        partial_workspace,
        profile=InstallProfile.WINDOWS_RELEASE,
    )
    partial_plan.install_root.mkdir(parents=True)
    with pytest.raises(LoopXInstallError) as partial:
        installer.install(
            partial_workspace,
            profile=InstallProfile.WINDOWS_RELEASE,
        )
    assert partial.value.code == "loopx_partial_install"
    assert partial.value.recovery

    workspace_file = tmp_path / "not-a-workspace"
    workspace_file.write_text("blocked\n", encoding="utf-8")
    with pytest.raises(LoopXInstallError) as unwritable:
        installer.install(
            workspace_file,
            profile=InstallProfile.WINDOWS_RELEASE,
        )
    assert unwritable.value.code == "loopx_workspace_unwritable"

    package = installer.package_lock.package
    assert isinstance(package, dict)
    package["runtime_dependency_imports"] = ["zyra_missing_loopx_dependency"]
    with pytest.raises(LoopXInstallError) as missing:
        installer.check_interpreter(Path(sys.executable))
    assert missing.value.code == "loopx_dependency_missing"
    assert missing.value.details["missing"] == ["zyra_missing_loopx_dependency"]


def test_installed_package_file_tamper_and_upgrade_drift_fail_closed(
    tmp_path: Path,
) -> None:
    installer = LoopXInstaller(PROJECT_ROOT)
    workspace = tmp_path / "workspace"
    installed = installer.install(
        workspace,
        profile=InstallProfile.WINDOWS_RELEASE,
    )
    install_root = Path(installed["install_root"])
    package_init = (
        install_root / str(installed["module_root"]) / "loopx" / "__init__.py"
    )
    package_init.write_text("__version__ = 'tampered'\n", encoding="utf-8")
    with pytest.raises(LoopXInstallError) as tampered:
        installer.validate_installed(install_root)
    assert tampered.value.code == "loopx_installed_file_tampered"

    receipt_path = install_root / "install-receipt.json"
    value = installed.copy()
    value["version"] = "9.9.9"
    unsigned = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "receipt_digest",
            "idempotent_replay",
            "interpreter",
            "artifact_verification",
        }
    }
    from zyra_integrations.loopx.install.manifest import stable_digest

    unsigned["receipt_digest"] = stable_digest(unsigned)
    receipt_path.write_text(
        json.dumps(unsigned, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(LoopXInstallError) as upgrade:
        installer.validate_installed(install_root)
    assert upgrade.value.code == "loopx_upgrade_required"


def test_package_lock_profiles_bind_one_source_and_no_external_runtime_path() -> None:
    lock = LoopXPackageLock.load(PROJECT_ROOT)
    assert lock.package["version"] == "0.2.4"
    assert (
        lock.package["source_commit"]
        == "8e79843704a40d8069a9cab4ede6edc6d29f671b"
    )
    assert {
        str(item["source_digest"]) for item in lock.profiles.values()
    } == {lock.source_digest}
    serialized = lock.path.read_text(encoding="utf-8").casefold()
    assert "../long-horizon-systems" not in serialized
    assert "g:\\agent-zoo\\long-horizon-systems" not in serialized
