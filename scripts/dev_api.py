from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT,
    ROOT / "apps" / "api",
    ROOT / "packages" / "code_index",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "core",
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "integrations" / "loopx_runtime",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "productization",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "skills",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "workspace",
)
for source_root in reversed(SOURCE_ROOTS):
    rendered = str(source_root)
    if rendered not in sys.path:
        sys.path.insert(0, rendered)


def _record_cli_daemon_runtime_identity() -> None:
    """Hand the real interpreter PID back to the Windows CLI launcher."""

    target_value = os.environ.get("ZYRA_CLI_DAEMON_RUNTIME_IDENTITY", "").strip()
    generation = os.environ.get("ZYRA_CLI_DAEMON_GENERATION", "").strip()
    if not target_value and not generation:
        return
    if not target_value or not generation:
        raise RuntimeError("incomplete Zyra CLI daemon runtime identity contract")
    target = Path(target_value).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema": "zyra.cli-daemon-runtime-identity.v1",
                "generation": generation,
                "pid": os.getpid(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


_record_cli_daemon_runtime_identity()

from apps.api.zyra_api.main import run


if __name__ == "__main__":
    run()
