from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS_ROOT = PROJECT_ROOT / "packages" / "integrations"
if str(INTEGRATIONS_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATIONS_ROOT))

from zyra_integrations.loopx.install import (
    InstallProfile,
    LoopXDoctor,
    LoopXInstallError,
    LoopXInstaller,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the pinned offline LoopX package, project-local profile and "
            "optional installed workspace."
        )
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--workspace", default="")
    parser.add_argument("--install-root", default="")
    parser.add_argument(
        "--profile",
        choices=["auto", *(item.value for item in InstallProfile)],
        default="auto",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--output", default="")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    project_root = Path(arguments.project_root).resolve()
    workspace = Path(arguments.workspace).resolve() if arguments.workspace else None
    install_root = (
        Path(arguments.install_root).resolve() if arguments.install_root else None
    )
    profile = None if arguments.profile == "auto" else arguments.profile
    try:
        install_receipt = None
        if arguments.install:
            if workspace is None:
                raise LoopXInstallError(
                    "--install requires an explicit project workspace.",
                    code="loopx_workspace_unwritable",
                )
            install_receipt = LoopXInstaller(project_root).install(
                workspace,
                profile=profile,
                python_executable=Path(arguments.python),
            )
            install_root = Path(str(install_receipt["install_root"]))
        report = LoopXDoctor(project_root).run(
            deep=arguments.deep,
            workspace_root=(
                workspace
                if arguments.deep and install_root is None and workspace is not None
                else None
            ),
            install_root=install_root,
            profile=profile,
            python_executable=Path(arguments.python),
        )
        result = {
            "schema": "zyra.loopx-install-verification/v1",
            "ready": report["ready"],
            "profile": profile or InstallProfile.current().value,
            "install": install_receipt,
            "doctor": report,
        }
    except LoopXInstallError as error:
        result = {
            "schema": "zyra.loopx-install-verification/v1",
            "ready": False,
            "error": error.to_dict(),
        }
    if arguments.output:
        output = Path(arguments.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ready"] is True else 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
