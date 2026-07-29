from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import venv
from pathlib import Path

from zyra_integrations.loopx.runtime import LoopXDoctor, LoopXRuntimeResolver
from zyra_productization.release.wheel import DeterministicWheelBuilder


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _venv_python(root: Path) -> Path:
    return (
        root / "Scripts" / "python.exe"
        if os.name == "nt"
        else root / "bin" / "python"
    )


def test_embedded_source_direct_import_cli_and_resource_provenance(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    resolution = LoopXRuntimeResolver(PROJECT_ROOT).receipt(workspace)
    report = LoopXDoctor(PROJECT_ROOT).run(
        deep=True,
        workspace_root=workspace,
        python_executable=Path(sys.executable),
    )

    assert report["ready"] is True
    assert resolution["version"] == "0.2.13"
    assert resolution["source_kind"] == "embedded_source"
    assert resolution["archive_extraction"] is False
    assert resolution["archive_fallback"] is False
    assert not (workspace / ".zyra" / "loopx" / "install").exists()
    import_check = next(
        item["details"]
        for item in report["checks"]
        if item["check"] == "import-and-cli-entry"
    )
    assert import_check["cli"] == "loopx 0.2.13"
    assert Path(import_check["module_origin"]).is_relative_to(
        Path(resolution["install_root"])
    )
    resources = next(
        item["details"]
        for item in report["checks"]
        if item["check"] == "extension-skill-resource-discovery"
    )
    assert resources["counts"]["extensions"] == 4
    assert resources["counts"]["skills"] == 5
    assert resources["counts"]["upstream_tests"] == 125


def test_built_zyra_wheel_imports_loopx_without_installer_or_checkout_cwd(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel_receipt = DeterministicWheelBuilder(PROJECT_ROOT).build(wheelhouse)
    wheel = wheelhouse / str(wheel_receipt["path"])
    environment_root = tmp_path / "environment"
    venv.EnvBuilder(
        with_pip=True,
        clear=True,
    ).create(environment_root)
    python = _venv_python(environment_root)
    install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert install.returncode == 0, install.stderr
    clean_cwd = tmp_path / "clean-cwd"
    clean_cwd.mkdir()
    probe_script = clean_cwd / "probe.py"
    probe_script.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            from importlib import metadata, resources
            from pathlib import Path

            import loopx
            import loopx.extensions.bundled as bundled
            from zyra_integrations.loopx.runtime import (
                LoopXDoctor,
            )

            workspace = Path.cwd() / "first-workspace"
            doctor = LoopXDoctor(Path.cwd()).run(
                deep=True,
                workspace_root=workspace,
                python_executable=Path(sys.executable),
            )
            resource_check = next(
                item["details"]
                for item in doctor["checks"]
                if item["check"] == "extension-skill-resource-discovery"
            )
            skill = resources.files("loopx.capabilities.auto_research").joinpath(
                "worker_skill/SKILL.md"
            )
            print(
                json.dumps(
                    {
                        "version": loopx.__version__,
                        "origin": loopx.__file__,
                        "extension": bundled.__file__,
                        "skill_exists": skill.is_file(),
                        "doctor_ready": doctor["ready"],
                        "installed_skills": resource_check[
                            "installed_skill_count"
                        ],
                        "installed_templates": resource_check[
                            "installed_template_count"
                        ],
                        "installed_extensions": resource_check[
                            "installed_external_extension_count"
                        ],
                        "retired_install_created": (
                            workspace / ".zyra" / "loopx" / "install"
                        ).exists(),
                        "distribution": metadata.packages_distributions().get(
                            "loopx"
                        ),
                    },
                    sort_keys=True,
                )
            )
            """
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    probe = subprocess.run(
        [str(python), str(probe_script)],
        cwd=clean_cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout)
    assert payload["version"] == "0.2.13"
    assert payload["skill_exists"] is True
    assert payload["doctor_ready"] is True
    assert payload["installed_skills"] == 4
    assert payload["installed_templates"] == 2
    assert payload["installed_extensions"] == 1
    assert payload["retired_install_created"] is False
    assert "zyra" in payload["distribution"]
    assert str(environment_root).casefold() in payload["origin"].casefold()
    cli = subprocess.run(
        [str(python), "-m", "loopx.cli", "--version"],
        cwd=clean_cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert cli.returncode == 0
    assert cli.stdout.strip() == "loopx 0.2.13"
    assert not (clean_cwd / ".zyra" / "loopx" / "install").exists()
