from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_root_pyproject_is_the_authoritative_python_workspace() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["build-system"]["build-backend"] == "setuptools.build_meta"
    assert "setuptools>=77" in project["build-system"]["requires"]
    assert "browser-use[core]==0.13.3" in project["project"]["dependencies"]

    expected_roots = {
        "apps/api",
        "packages/code_index",
        "packages/commands",
        "packages/core",
        "packages/evaluation",
        "packages/integrations",
        "packages/memory",
        "packages/orchestration",
        "packages/runtime",
        "packages/scheduler",
        "packages/skills",
        "packages/symbolic",
        "packages/workers",
        "packages/workspace",
    }
    discovery = project["tool"]["setuptools"]["packages"]["find"]
    assert set(discovery["where"]) == expected_roots
    assert discovery["include"] == ["zyra_*"]
    assert discovery["namespaces"] is False
    assert project["tool"]["setuptools"]["package-data"]["zyra_integrations"] == [
        "data/*.json"
    ]


def test_declared_workspace_packages_are_importable_without_test_path_bootstrap() -> None:
    modules = (
        "zyra_api",
        "zyra_code_index",
        "zyra_commands",
        "zyra_core",
        "zyra_evaluation",
        "zyra_integrations",
        "zyra_memory",
        "zyra_orchestration",
        "zyra_runtime",
        "zyra_scheduler",
        "zyra_skills",
        "zyra_symbolic",
        "zyra_workers",
        "zyra_workspace",
    )
    original_path = list(sys.path)
    try:
        sys.path[:] = [
            item
            for item in sys.path
            if Path(item or ".").resolve() != ROOT
            and not str(Path(item or ".").resolve()).startswith(str(ROOT / "packages"))
            and not str(Path(item or ".").resolve()).startswith(str(ROOT / "apps"))
        ]
        for name in modules:
            sys.modules.pop(name, None)
            imported = importlib.import_module(name)
            assert imported.__file__
    finally:
        sys.path[:] = original_path
