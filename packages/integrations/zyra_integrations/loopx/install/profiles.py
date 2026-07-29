from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..runtime.manifest import LoopXPackageLock
from ..runtime.resolver import LoopXRuntimeResolver


class InstallProfile(StrEnum):
    """Historical profile names retained only as input compatibility."""

    LINUX_WSL = "linux_wsl_upstream_semantics"
    WINDOWS_RELEASE = "windows_release_offline_wheel"
    EMBEDDED_SOURCE = "pinned_embedded_source"

    @classmethod
    def current(cls) -> "InstallProfile":
        return cls.EMBEDDED_SOURCE

    @classmethod
    def parse(cls, value: str | "InstallProfile" | None) -> "InstallProfile":
        if value is None or str(value) in {"", "auto"}:
            return cls.EMBEDDED_SOURCE
        selected = cls(str(value))
        # Old callers are intentionally cut over to the one embedded source.
        return cls.EMBEDDED_SOURCE if selected is not cls.EMBEDDED_SOURCE else selected


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
            "schema": "zyra.loopx-embedded-runtime-plan/v1",
            "profile": self.profile.value,
            "workspace_root": str(self.workspace_root),
            "custody_root": str(self.custody_root),
            "install_root": str(self.install_root),
            "state_root": str(self.state_root),
            "runtime_root": str(self.runtime_root),
            "python_executable": str(self.python_executable),
            "artifact_name": self.artifact_name,
            "source_digest": self.source_digest,
            "network_access": False,
            "archive_extraction": False,
            "archive_fallback": False,
            "user_home_write": False,
            "system_path_write": False,
            "disabled_user_surfaces": list(self.disabled_user_surfaces),
            "environment": dict(self.environment),
        }


class ProfileResolver:
    """Map historical install calls to the single embedded runtime locator."""

    def __init__(self, package_lock: LoopXPackageLock) -> None:
        self.package_lock = package_lock

    def resolve(
        self,
        workspace_root: Path,
        *,
        profile: str | InstallProfile | None = None,
        python_executable: Path | None = None,
    ) -> ProfilePlan:
        del profile
        workspace = workspace_root.resolve()
        custody = (workspace / ".zyra" / "loopx").resolve()
        resolution = LoopXRuntimeResolver(
            self.package_lock.package_root
        ).resolve(verify=False)
        retired = custody / "install"
        disabled = (
            "archive_installer",
            "archive_fallback",
            "root_source_fallback",
            "user_level_loopx",
            "user_shell_profile",
            "user_codex_skill",
            "user_claude_adapter",
            "global_slash_command",
            "system_path",
            "tmux_dashboard",
            "canary_symlink",
        )
        return ProfilePlan(
            profile=InstallProfile.EMBEDDED_SOURCE,
            workspace_root=workspace,
            custody_root=custody,
            install_root=resolution.runtime_root,
            state_root=custody / "state",
            runtime_root=custody / "runtime",
            bin_root=retired / "bin",
            releases_root=retired / "releases",
            skills_root=resolution.runtime_root / "skills",
            manual_root=resolution.runtime_root / "man",
            shell_profile=retired / "profile" / "loopx.env",
            python_executable=(python_executable or Path(sys.executable)).resolve(),
            artifact_name="",
            source_digest=resolution.source_digest,
            environment=(
                ("PYTHONDONTWRITEBYTECODE", "1"),
                ("ZYRA_LOOPX_RUNTIME_SOURCE", "embedded"),
            ),
            disabled_user_surfaces=disabled,
        )


__all__ = ["InstallProfile", "ProfilePlan", "ProfileResolver"]
