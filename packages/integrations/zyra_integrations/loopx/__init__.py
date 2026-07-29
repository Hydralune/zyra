"""Pinned embedded LoopX supplementary runtime integration."""

from typing import Any

from .install import (
    LOOPX_SOURCE_COMMIT,
    LOOPX_SOURCE_REF,
    LOOPX_SOURCE_TREE_COMMIT,
    LOOPX_VERSION,
    InstallProfile,
    LoopXDoctor,
    LoopXInstallError,
    LoopXInstaller,
    LoopXPackageLock,
)


def __getattr__(name: str) -> Any:
    if name in {"LoopXControlRuntime", "task_goal_id"}:
        from .control import LoopXControlRuntime, task_goal_id

        return {
            "LoopXControlRuntime": LoopXControlRuntime,
            "task_goal_id": task_goal_id,
        }[name]
    raise AttributeError(name)

__all__ = [
    "LOOPX_SOURCE_COMMIT",
    "LOOPX_SOURCE_REF",
    "LOOPX_SOURCE_TREE_COMMIT",
    "LOOPX_VERSION",
    "InstallProfile",
    "LoopXDoctor",
    "LoopXInstallError",
    "LoopXInstaller",
    "LoopXPackageLock",
    "LoopXControlRuntime",
    "task_goal_id",
]
