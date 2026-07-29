from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORMAL_RUNTIME = ROOT / "packages" / "runtime" / "claude-runtime"
FORMAL_ENTRYPOINT = ROOT / "apps" / "code-worker" / "src" / "main.ts"


def retirement_status() -> dict[str, object]:
    checks = {
        "formal_runtime_package": (FORMAL_RUNTIME / "package.json").is_file(),
        "formal_runtime_source": (FORMAL_RUNTIME / "src").is_dir(),
        "formal_runtime_identity": (FORMAL_RUNTIME / "zyra-source.json").is_file(),
        "canonical_code_worker_entrypoint": FORMAL_ENTRYPOINT.is_file(),
    }
    return {
        "ok": all(checks.values()),
        "status": "retired",
        "reason": "legacy_source_pool_retired",
        "writer_available": False,
        "external_source_workspace_required": False,
        "fallback_available": False,
        "current_runtime_owner": "packages/runtime/claude-runtime",
        "canonical_entrypoint": "apps/code-worker/src/main.ts",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report retirement of the first-stage source extraction workflow. "
            "This command never copies or writes runtime sources."
        )
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        help="Only 'status' is supported; historical extraction commands fail closed.",
    )
    parser.add_argument("--json", action="store_true")
    arguments, unknown = parser.parse_known_args()
    payload = retirement_status()
    if arguments.command != "status" or unknown:
        payload = {
            **payload,
            "ok": False,
            "error": "legacy_source_extraction_command_retired",
            "requested_command": arguments.command,
            "unknown_arguments": unknown,
        }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
