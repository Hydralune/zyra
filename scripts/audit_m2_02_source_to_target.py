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
ROLE_POLICY = {
    "M2-S02A-01": {
        "zyra": "primary_implementation",
        "opencode": "reference_only",
        "OpenHands": "reference_only",
        "oh-my-pi": "reference_only",
        "langgraph": "conformance_only",
    },
    "M2-S02A-02": {
        "zyra": "primary_implementation",
        "opencode": "reference_only",
        "OpenHands": "reference_only",
        "langgraph": "conformance_only",
    },
    "M2-S02B-01": {
        "opencode": "primary_implementation",
        "OpenHands": "supplementary_implementation",
        "browser-use": "supplementary_implementation",
        "oh-my-pi": "conformance_only",
        "agent-framework": "conformance_only",
        "langgraph": "conformance_only",
    },
    "M2-S02B-02": {
        "opencode": "primary_implementation",
        "OpenHands": "supplementary_implementation",
        "browser-use": "supplementary_implementation",
        "agent-framework": "conformance_only",
        "oh-my-pi": "reference_only",
        "langgraph": "conformance_only",
    },
}
EXPECTED_COUNTS = {
    unit: len(decisions) for unit, decisions in ROLE_POLICY.items()
}
CANONICAL_ROLES = {
    "primary_implementation",
    "supplementary_implementation",
    "conformance_only",
    "reference_only",
    "experimental",
    "deferred",
    "rejected",
}
PRODUCTION_ROLES = {
    "primary_implementation",
    "supplementary_implementation",
}
LANGUAGE_BY_SUFFIX = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def resolve_commit(reference: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{reference}^{{commit}}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise ValueError(
            f"cannot resolve target commit {reference}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


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
        elif role not in CANONICAL_ROLES:
            errors.append(f"{unit}:{identity}:invalid-source-role:{role}")
        if not str(metadata.get("source_commit") or ""):
            errors.append(f"{unit}:{identity}:missing-source-commit")
        source_language = str(metadata.get("source_language") or "").strip()
        target_language = str(metadata.get("target_language") or "").strip()
        migration_mode = str(metadata.get("migration_mode") or "").strip()
        if not source_language:
            errors.append(f"{unit}:{identity}:missing-source-language")
        if not target_language:
            errors.append(f"{unit}:{identity}:missing-target-language")
        if not migration_mode:
            errors.append(f"{unit}:{identity}:missing-migration-mode")
        for label, language in (
            ("source", source_language),
            ("target", target_language),
        ):
            normalized = language.casefold()
            if normalized in {"mixed", "unknown"} or "/" in normalized:
                errors.append(
                    f"{unit}:{identity}:{label}-language-not-exact:"
                    f"{language}"
                )
        if bool(metadata.get("root_source_runtime_dependency")):
            errors.append(f"{unit}:{identity}:root-source-runtime-dependency")
        if source_repo.casefold() == "openclaw":
            errors.append(f"{unit}:{identity}:openclaw-forward-entry")
        production = role in PRODUCTION_ROLES
        if production:
            if source_language.casefold() == "none":
                errors.append(f"{unit}:{identity}:production-source-language-none")
            if target_language.casefold() == "none":
                errors.append(f"{unit}:{identity}:production-target-language-none")
            if (
                "same_language" in migration_mode.casefold()
                and source_language.casefold() != target_language.casefold()
            ):
                errors.append(
                    f"{unit}:{identity}:same-language-mismatch:"
                    f"{source_language}->{target_language}"
                )
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
                suffix_language = LANGUAGE_BY_SUFFIX.get(
                    Path(path).suffix.casefold()
                )
                if (
                    suffix_language is not None
                    and suffix_language != target_language.casefold()
                ):
                    errors.append(
                        f"{unit}:{identity}:production-target-language:"
                        f"{path}:{suffix_language}:expected:{target_language}"
                    )
            else:
                conformance_bindings += 1

    for unit, expected_policy in ROLE_POLICY.items():
        roles = source_roles.get(unit, Counter())
        if roles["primary_implementation"] != 1:
            errors.append(
                f"{unit}:primary-count:{roles['primary_implementation']}"
            )
        supplementary_count = roles["supplementary_implementation"]
        if supplementary_count > 2:
            errors.append(
                f"{unit}:supplementary-count:{supplementary_count}:maximum:2"
            )
        actual_policy = {
            str(item.get("source_repo") or ""): str(
                (item.get("metadata") or {}).get("source_role") or ""
            )
            for item in entries
            if str(item.get("owner_unit") or "") == unit
        }
        if actual_policy != expected_policy:
            errors.append(
                f"{unit}:source-role-policy:{sorted(actual_policy.items())}:"
                f"expected:{sorted(expected_policy.items())}"
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
    parser.add_argument("--target", default="HEAD")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    document = json.loads(args.ledger.resolve().read_text(encoding="utf-8"))
    target = resolve_commit(args.target)
    result = audit(document, target)
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
