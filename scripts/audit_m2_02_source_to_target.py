from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
DEFAULT_TARGET = "f3302a3c5366218068cb8acf9790c973e4b62033"
EXPECTED_COUNTS = {
    "M2-S02A-01": 5,
    "M2-S02A-02": 4,
    "M2-S02B-01": 6,
    "M2-S02B-02": 6,
}


def git_file_exists(commit: str, path: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:{path}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def selected_entries(document: Any) -> list[dict[str, Any]]:
    entries = document if isinstance(document, list) else document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("ledger seed must be a list or contain entries")
    return [
        item
        for item in entries
        if str(item.get("owner_unit") or "") in EXPECTED_COUNTS
    ]


def audit(document: Any, target: str) -> dict[str, Any]:
    entries = selected_entries(document)
    counts = Counter(str(item.get("owner_unit") or "") for item in entries)
    errors: list[str] = []
    warnings: list[str] = []
    ids: set[str] = set()
    source_roles: dict[str, Counter[str]] = {}
    production_bindings = 0
    conformance_bindings = 0

    for unit, expected in EXPECTED_COUNTS.items():
        if counts[unit] != expected:
            errors.append(
                f"{unit}:entry-count:{counts[unit]}:expected:{expected}"
            )

    for item in entries:
        unit = str(item.get("owner_unit") or "")
        identity = str(item.get("ledger_id") or "")
        metadata = item.get("metadata") or {}
        role = str(metadata.get("source_role") or "")
        source_repo = str(item.get("source_repo") or "")
        source_roles.setdefault(unit, Counter())[role] += 1
        if not identity:
            errors.append(f"{unit}:missing-ledger-id")
        elif identity in ids:
            errors.append(f"{unit}:duplicate-ledger-id:{identity}")
        ids.add(identity)
        if not role:
            errors.append(f"{unit}:{identity}:missing-source-role")
        if not str(metadata.get("source_commit") or ""):
            errors.append(f"{unit}:{identity}:missing-source-commit")
        if bool(metadata.get("root_source_runtime_dependency")):
            errors.append(f"{unit}:{identity}:root-source-runtime-dependency")
        if unit == "M2-S02B-02" and source_repo.lower() == "openclaw":
            errors.append(f"{unit}:{identity}:openclaw-forward-entry")
        production = role in {
            "primary_implementation",
            "supplementary_implementation",
        }
        for binding in item.get("target_bindings") or []:
            path = str(binding.get("target_path") or "")
            if not path:
                errors.append(f"{unit}:{identity}:empty-target")
                continue
            if not git_file_exists(target, path):
                errors.append(f"{unit}:{identity}:missing-target:{path}")
            if production:
                production_bindings += 1
                if not bool(binding.get("required_for_main_path")):
                    errors.append(
                        f"{unit}:{identity}:production-target-not-required:{path}"
                    )
            else:
                conformance_bindings += 1

    for unit in EXPECTED_COUNTS:
        roles = source_roles.get(unit, Counter())
        if roles["primary_implementation"] != 1:
            errors.append(
                f"{unit}:primary-count:{roles['primary_implementation']}"
            )
        if unit == "M2-S02B-02":
            if roles["supplementary_implementation"] != 2:
                errors.append(
                    f"{unit}:supplementary-count:"
                    f"{roles['supplementary_implementation']}"
                )
            repos = {
                str(item.get("source_repo") or "")
                for item in entries
                if str(item.get("owner_unit") or "") == unit
                and str((item.get("metadata") or {}).get("source_role") or "")
                in {"primary_implementation", "supplementary_implementation"}
            }
            expected_repos = {"opencode", "OpenHands", "browser-use"}
            if repos != expected_repos:
                errors.append(
                    f"{unit}:production-source-repos:"
                    f"{sorted(repos)}:expected:{sorted(expected_repos)}"
                )

    if source_roles["M2-S02B-01"]["supplementary_implementation"] > 2:
        warnings.append(
            "M2-S02B-01 is a protected historical slice with three "
            "supplementary entries across separate projection capability "
            "chains; this aggregate does not rewrite it."
        )

    return {
        "schema": "zyra.m2-02-source-to-target-audit/v1",
        "target_commit": target,
        "units": list(EXPECTED_COUNTS),
        "entry_counts": dict(sorted(counts.items())),
        "source_roles": {
            unit: dict(sorted(roles.items()))
            for unit, roles in sorted(source_roles.items())
        },
        "entry_count": len(entries),
        "unique_ledger_id_count": len(ids),
        "production_target_binding_count": production_bindings,
        "conformance_or_reference_binding_count": conformance_bindings,
        "missing_target_count": sum(
            1 for error in errors if ":missing-target:" in error
        ),
        "openclaw_forward_entry_count": sum(
            1 for error in errors if error.endswith(":openclaw-forward-entry")
        ),
        "warnings": warnings,
        "errors": errors,
        "ok": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    document = json.loads(args.ledger.resolve().read_text(encoding="utf-8"))
    result = audit(document, args.target)
    if args.summary:
        print(f"target_commit={result['target_commit']}")
        print(f"entry_count={result['entry_count']}")
        print(
            "production_target_binding_count="
            f"{result['production_target_binding_count']}"
        )
        print(
            "conformance_or_reference_binding_count="
            f"{result['conformance_or_reference_binding_count']}"
        )
        print(f"missing_target_count={result['missing_target_count']}")
        print(
            "openclaw_forward_entry_count="
            f"{result['openclaw_forward_entry_count']}"
        )
        print(f"warning_count={len(result['warnings'])}")
        print(f"error_count={len(result['errors'])}")
        print(f"ok={str(result['ok']).lower()}")
        for warning in result["warnings"]:
            print(f"warning={warning}")
        for error in result["errors"]:
            print(f"error={error}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
