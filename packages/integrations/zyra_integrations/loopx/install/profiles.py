from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .errors import LoopXInstallError
from .manifest import LoopXPackageLock


class InstallProfile(StrEnum):
    LINUX_WSL = "linux_wsl_upstream_semantics"
    WINDOWS_RELEASE = "windows_release_offline_wheel"

    @classmethod
    def current(cls) -> "InstallProfile":
        return cls.WINDOWS_RELEASE if os.name == "nt" else cls.LINUX_WSL

    @classmethod
    def parse(cls, value: str | "InstallProfile" | None) -> "InstallProfile":
        if value is None or str(value) in {"", "auto"}:
            return cls.current()
        try:
            return cls(str(value))
        except ValueError as error:
            raise LoopXInstallError(
                "Requested LoopX install profile is unsupported.",
                code="loopx_profile_unsupported",
                details={
                    "profile": str(value),
                    "supported": [item.value for item in cls],
                },
            ) from error


@dataclass(frozen=True, slots=True)
class ProfilePlan:
    profile: InstallProfile
    workspace_root: Path
    custody_root: Path
    install_root: Path
    state_root: Path
    runtime_root: Path
    bin_root: Path
    releases_root: Path
    skills_root: Path
    manual_root: Path
    shell_profile: Path
    python_executable: Path
    artifact_name: str
    source_digest: str
    environment: tuple[tuple[str, str], ...]
    disabled_user_surfaces: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.loopx-install-plan/v1",
            "profile": self.profile.value,
            "platform": platform.system().lower(),
            "workspace_root": str(self.workspace_root),
            "custody_root": str(self.custody_root),
            "install_root": str(self.install_root),
            "state_root": str(self.state_root),
            "runtime_root": str(self.runtime_root),
            "bin_root": str(self.bin_root),
            "releases_root": str(self.releases_root),
            "skills_root": str(self.skills_root),
            "manual_root": str(self.manual_root),
            "shell_profile": str(self.shell_profile),
            "python_executable": str(self.python_executable),
            "artifact_name": self.artifact_name,
            "source_digest": self.source_digest,
            "network_access": False,
            "user_home_write": False,
            "system_path_write": False,
            "disabled_user_surfaces": list(self.disabled_user_surfaces),
            "environment": dict(self.environment),
        }


class ProfileResolver:
    """Resolve both ADR install profiles into project-local custody paths."""

    def __init__(self, package_lock: LoopXPackageLock) -> None:
        self.package_lock = package_lock

    def resolve(
        self,
        workspace_root: Path,
        *,
        profile: str | InstallProfile | None = None,
        python_executable: Path | None = None,
    ) -> ProfilePlan:
        selected = InstallProfile.parse(profile)
        workspace = workspace_root.resolve()
        custody = (workspace / ".zyra" / "loopx").resolve()
        install_root = (custody / "install" / selected.value).resolve()
        state_root = (custody / "state").resolve()
        runtime_root = (custody / "runtime").resolve()
        bin_root = (install_root / "bin").resolve()
        releases_root = (install_root / "releases").resolve()
        skills_root = (install_root / "disabled-user-skills").resolve()
        manual_root = (install_root / "manual").resolve()
        shell_profile = (install_root / "profile" / "loopx.env").resolve()
        python = (python_executable or Path(sys.executable)).resolve()
        profile_contract = self.package_lock.profiles.get(selected.value)
        if not isinstance(profile_contract, dict):
            raise LoopXInstallError(
                "Requested LoopX profile is not present in the package lock.",
                code="loopx_profile_unsupported",
                details={"profile": selected.value},
            )
        artifact_name = str(profile_contract.get("artifact") or "")
        if artifact_name not in self.package_lock.artifacts:
            raise LoopXInstallError(
                "Requested LoopX profile names an unknown artifact.",
                code="loopx_package_lock_invalid",
                details={
                    "profile": selected.value,
                    "artifact": artifact_name,
                },
            )
        roots = (
            custody,
            install_root,
            state_root,
            runtime_root,
            bin_root,
            releases_root,
            skills_root,
            manual_root,
            shell_profile.parent,
        )
        for root in roots:
            try:
                root.relative_to(workspace)
            except ValueError as error:
                raise LoopXInstallError(
                    "LoopX profile path escapes the project workspace.",
                    code="loopx_user_write_forbidden",
                    details={"workspace": str(workspace), "path": str(root)},
                ) from error
        disabled = (
            "user_shell_profile",
            "user_codex_skill",
            "user_claude_adapter",
            "global_slash_command",
            "system_path",
            "tmux_dashboard",
            "canary_symlink",
        )
        environment = (
            ("LOOPX_BIN_DIR", str(bin_root)),
            ("LOOPX_RELEASES_DIR", str(releases_root)),
            ("LOOPX_SKILLS_DIR", str(skills_root)),
            ("LOOPX_MAN_ROOT", str(manual_root)),
            ("LOOPX_RUNTIME_ROOT", str(runtime_root)),
            ("LOOPX_SHELL_PROFILE", str(shell_profile)),
            ("LOOPX_INSTALL_SKILL", "0"),
            ("LOOPX_INSTALL_SLASH_COMMANDS", "0"),
            ("LOOPX_INSTALL_CLAUDE", "0"),
            ("LOOPX_INSTALL_CANARY", "0"),
            ("LOOPX_PROMOTE_DEFAULT", "1"),
            ("LOOPX_PROMOTION_MODE", "zyra_pinned_release"),
            (
                "LOOPX_RESOLVED_SOURCE_GIT_COMMIT",
                str(self.package_lock.package["source_commit"]),
            ),
        )
        return ProfilePlan(
            profile=selected,
            workspace_root=workspace,
            custody_root=custody,
            install_root=install_root,
            state_root=state_root,
            runtime_root=runtime_root,
            bin_root=bin_root,
            releases_root=releases_root,
            skills_root=skills_root,
            manual_root=manual_root,
            shell_profile=shell_profile,
            python_executable=python,
            artifact_name=artifact_name,
            source_digest=self.package_lock.source_digest,
            environment=environment,
            disabled_user_surfaces=disabled,
        )


__all__ = ["InstallProfile", "ProfilePlan", "ProfileResolver"]
