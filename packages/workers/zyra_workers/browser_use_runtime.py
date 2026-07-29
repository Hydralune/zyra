from __future__ import annotations

import os
import importlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class BrowserUseRuntimePaths:
    root: Path
    config_dir: Path
    cache_dir: Path
    temp_dir: Path
    profiles_dir: Path


@dataclass(frozen=True, slots=True)
class BrowserUseRuntimeHealth:
    importable: bool
    environment_configured: bool
    paths: BrowserUseRuntimePaths
    modules: dict[str, bool] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)
    error_type: str | None = None
    error: str | None = None

    def as_metadata(self) -> dict[str, str]:
        failed_modules = sorted(name for name, ok in self.modules.items() if not ok)
        return {
            "browser_use_python_importable": str(self.importable).lower(),
            "browser_use_environment_configured": str(self.environment_configured).lower(),
            "browser_use_runtime_root": str(self.paths.root),
            "browser_use_config_dir": str(self.paths.config_dir),
            "browser_use_cache_dir": str(self.paths.cache_dir),
            "browser_use_temp_dir": str(self.paths.temp_dir),
            "browser_use_failed_modules": ",".join(failed_modules),
            "browser_use_agent_class": self.classes.get("Agent", ""),
            "browser_use_agent_history_class": self.classes.get("AgentHistoryList", ""),
            "browser_use_session_class": self.classes.get("BrowserSession", ""),
            "browser_use_profile_class": self.classes.get("BrowserProfile", ""),
            "browser_use_tools_class": self.classes.get("Tools", ""),
            "browser_use_llm_factory": self.classes.get("get_llm_by_name", ""),
            "browser_use_runtime_error_type": self.error_type or "",
            "browser_use_runtime_error": self.error or "",
            "browser_runtime_backend": "zyra-browser-productized",
            "browser_runtime_source_pool_import_required": "false",
        }


def configure_browser_use_environment(project_root: str | Path) -> BrowserUseRuntimePaths:
    """Keep browser runtime config/cache/profile writes inside the Zyra project."""

    project = Path(project_root).resolve()
    runtime_root = project / "tmp" / "browser-use-runtime"
    config_dir = runtime_root / "config"
    cache_dir = runtime_root / "cache"
    temp_dir = runtime_root / "temp"
    profiles_dir = config_dir / "profiles"
    for path in (runtime_root, config_dir, cache_dir, temp_dir, profiles_dir):
        path.mkdir(parents=True, exist_ok=True)

    _set_project_env_path("BROWSER_USE_CONFIG_DIR", config_dir, project)
    _set_project_env_path("XDG_CACHE_HOME", cache_dir, project)
    _set_project_env_path("TEMP", temp_dir, project)
    _set_project_env_path("TMP", temp_dir, project)
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    os.environ.setdefault("BROWSER_USE_VERSION_CHECK", "false")
    os.environ.setdefault("BROWSER_USE_DISABLE_EXTENSIONS", "1")

    return BrowserUseRuntimePaths(
        root=runtime_root,
        config_dir=Path(os.environ["BROWSER_USE_CONFIG_DIR"]).expanduser().resolve(),
        cache_dir=Path(os.environ["XDG_CACHE_HOME"]).expanduser().resolve(),
        temp_dir=Path(os.environ["TEMP"]).expanduser().resolve(),
        profiles_dir=Path(os.environ["BROWSER_USE_CONFIG_DIR"]).expanduser().resolve() / "profiles",
    )


def inspect_browser_use_runtime(project_root: str | Path) -> BrowserUseRuntimeHealth:
    paths = configure_browser_use_environment(project_root)
    try:
        importlib.import_module("browser_use")
    except Exception as error:
        return BrowserUseRuntimeHealth(
            importable=False,
            environment_configured=True,
            paths=paths,
            modules={"browser_use": False},
            error_type=type(error).__name__,
            error=str(error),
        )
    return BrowserUseRuntimeHealth(
        importable=True,
        environment_configured=True,
        paths=paths,
        modules={
            "zyra_browser_session_runtime": True,
            "zyra_browser_cdp_transport": True,
            "zyra_browser_profile_store": True,
        },
        classes={
            "Agent": "Agent",
            "AgentHistoryList": "AgentHistoryList",
            "BrowserRuntime": "BrowserRuntime",
            "BrowserSessionCommand": "BrowserSessionCommand",
            "BrowserSessionRef": "BrowserSessionRef",
            "BrowserSession": "BrowserSession",
            "BrowserProfile": "BrowserProfile",
            "Tools": "Tools",
            "get_llm_by_name": "get_llm_by_name",
            "NavigateAction": "NavigateAction",
            "ClickElementAction": "ClickElementAction",
            "InputTextAction": "InputTextAction",
            "UploadFileAction": "UploadFileAction",
            "SearchPageAction": "SearchPageAction",
            "ScrollAction": "ScrollAction",
            "SendKeysAction": "SendKeysAction",
            "ScreenshotAction": "ScreenshotAction",
            "SaveAsPdfAction": "SaveAsPdfAction",
            "NoParamsAction": "NoParamsAction",
        },
    )


def _set_project_env_path(name: str, value: Path, project_root: Path) -> None:
    current = os.environ.get(name)
    if current:
        try:
            resolved = Path(current).expanduser().resolve()
            resolved.relative_to(project_root)
            return
        except (OSError, ValueError):
            pass
    os.environ[name] = str(value)


def browser_use_runtime_metadata(health: BrowserUseRuntimeHealth | None) -> dict[str, str]:
    if health is None:
        return {
            "browser_use_python_importable": "false",
            "browser_use_environment_configured": "false",
            "browser_use_runtime_error_type": "not_checked",
            "browser_use_runtime_error": "",
        }
    return health.as_metadata()


def browser_use_health_summary(health: BrowserUseRuntimeHealth) -> dict[str, Any]:
    return {
        "importable": health.importable,
        "environment_configured": health.environment_configured,
        "paths": {
            "root": str(health.paths.root),
            "config_dir": str(health.paths.config_dir),
            "cache_dir": str(health.paths.cache_dir),
            "temp_dir": str(health.paths.temp_dir),
            "profiles_dir": str(health.paths.profiles_dir),
        },
        "modules": dict(health.modules),
        "classes": dict(health.classes),
        "error_type": health.error_type,
        "error": health.error,
    }


def find_browser_executable(extra_candidates: Iterable[str | Path] = ()) -> Path | None:
    configured = os.environ.get("BROWSER_USE_CHROME_PATH")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(Path(candidate) for candidate in extra_candidates if str(candidate))
    candidates.extend(
        [
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        ]
    )
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate.resolve()
        except OSError:
            continue
    return None
