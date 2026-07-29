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

from zyra_integrations.loopx.runtime import (
    LoopXDoctor,
    LoopXRuntimeError,
    LoopXRuntimeResolver,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the pinned embedded LoopX runtime without installation, "
            "archive extraction, network access, or user-level fallback."
        )
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--workspace", default="")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--output", default="")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    project_root = Path(arguments.project_root).resolve()
    workspace = (
        Path(arguments.workspace).resolve()
        if arguments.workspace
        else project_root
    )
    try:
        runtime = LoopXRuntimeResolver(project_root).receipt(workspace)
        doctor = LoopXDoctor(project_root).run(
            deep=arguments.deep,
            workspace_root=workspace,
            python_executable=Path(arguments.python),
        )
        result = {
            "schema": "zyra.loopx-runtime-verification/v1",
            "ready": doctor["ready"],
            "runtime": runtime,
            "doctor": doctor,
            "installer_invoked": False,
            "archive_extraction": False,
        }
    except LoopXRuntimeError as error:
        result = {
            "schema": "zyra.loopx-runtime-verification/v1",
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
