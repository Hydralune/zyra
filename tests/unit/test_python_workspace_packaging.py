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
        "packages/integrations/loopx_runtime",
        (
            "packages/integrations/loopx_runtime/packages/"
            "loopx-finance-value-discovery/src"
        ),
        "packages/memory",
        "packages/orchestration",
        "packages/productization",
        "packages/runtime",
        "packages/scheduler",
        "packages/skills",
        "packages/symbolic",
        "packages/workers",
        "packages/workspace",
    }
    discovery = project["tool"]["setuptools"]["packages"]["find"]
    assert set(discovery["where"]) == expected_roots
    assert discovery["include"] == [
        "zyra_*",
        "loopx*",
        "loopx_finance_*",
    ]
    assert discovery["namespaces"] is True
    assert project["tool"]["setuptools"]["package-data"]["zyra_integrations"] == [
        "data/*.json",
        "loopx/runtime/*.json",
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
    original_modules = {name: sys.modules.get(name) for name in modules}
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
        for name, original in original_modules.items():
            sys.modules.pop(name, None)
            if original is not None:
                sys.modules[name] = original


def test_test_bootstrap_prefers_active_source_tree() -> None:
    modules = (
        "zyra_api",
        "zyra_evaluation",
        "zyra_orchestration",
        "zyra_symbolic",
    )
    for name in modules:
        imported = importlib.import_module(name)
        source = Path(imported.__file__ or "").resolve()
        assert source.is_relative_to(ROOT), (name, source, ROOT)
