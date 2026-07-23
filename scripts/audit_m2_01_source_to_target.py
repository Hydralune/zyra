from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNERS = {
    "M2-S01A-01",
    "M2-S01A-02",
    "M2-S01B-01",
    "M2-S01B-02",
}
REPOSITORIES = {
    "opencode": WORKSPACE / "opencode",
    "OpenHands": WORKSPACE / "OpenHands",
    "claude-code-best": WORKSPACE / "claude-code-best",
    "oh-my-pi": WORKSPACE / "oh-my-pi",
    "agent-framework": WORKSPACE / "agent-framework",
    "agentscope": WORKSPACE / "agentscope",
}


def expand_braces(value: str) -> list[str]:
    start = value.find("{")
    if start < 0:
        return [value]
    end = value.find("}", start + 1)
    if end < 0:
        return [value]
    prefix = value[:start]
    suffix = value[end + 1 :]
    result: list[str] = []
    for choice in value[start + 1 : end].split(","):
        result.extend(expand_braces(f"{prefix}{choice}{suffix}"))
    return result


def source_paths(value: str) -> list[str]:
    result: list[str] = []
    for group in value.split(";"):
        normalized = group.strip().replace("\\", "/")
        if normalized:
            result.extend(expand_braces(normalized))
    return result


def git_commit_exists(repository: Path, commit: str) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


def git_object_exists(repository: Path, commit: str, path: str) -> bool:
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


def resolve_commit(repository: Path, commit: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", f"{commit}^{{commit}}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def audit(implementation_commit: str) -> dict[str, Any]:
    implementation_commit = resolve_commit(ROOT, implementation_commit)
    document = json.loads(LEDGER.read_text(encoding="utf-8"))
    entries = [
        item
        for item in document["entries"]
        if str(item.get("owner_unit") or "") in OWNERS
    ]
    findings: list[dict[str, Any]] = []
    source_file_count = 0
    target_count = 0
    production_entries = 0
    conformance_entries = 0
    canonical_projection_owners: set[str] = set()
    for item in entries:
        metadata = item.get("metadata") or {}
        role = str(metadata.get("source_role") or "")
        production = role in {
            "primary_implementation",
            "supplementary_implementation",
        }
        if production:
            production_entries += 1
        else:
            conformance_entries += 1
        projection_owner = str(metadata.get("canonical_ui_projection_owner") or "")
        if projection_owner and not projection_owner.startswith("deferred"):
            canonical_projection_owners.add(projection_owner)
        repository = str(item.get("source_repo") or "")
        repository_root = REPOSITORIES.get(repository)
        source_commit = str(metadata.get("source_commit") or "").strip()
        if repository_root is None or not repository_root.is_dir():
            findings.append(
                {
                    "code": "SOURCE_REPOSITORY_MISSING",
                    "ledger_id": item.get("ledger_id"),
                    "source_repo": repository,
                    "blocking": production,
                }
            )
        elif not source_commit or not git_commit_exists(repository_root, source_commit):
            findings.append(
                {
                    "code": "SOURCE_COMMIT_MISSING",
                    "ledger_id": item.get("ledger_id"),
                    "source_repo": repository,
                    "source_commit": source_commit,
                    "blocking": production,
                }
            )
        else:
            for relative in source_paths(str(item.get("source_path") or "")):
                if git_object_exists(repository_root, source_commit, relative):
                    source_file_count += 1
                    continue
                findings.append(
                    {
                        "code": (
                            "SOURCE_FILE_MISSING"
                            if production
                            else "CONFORMANCE_DESCRIPTOR_NOT_FILE"
                        ),
                        "ledger_id": item.get("ledger_id"),
                        "source_repo": repository,
                        "source_path": relative,
                        "blocking": production,
                    }
                )
        for binding in item.get("target_bindings") or []:
            target = str(binding.get("target_path") or "")
            if not git_object_exists(ROOT, implementation_commit, target):
                findings.append(
                    {
                        "code": "TARGET_MISSING_AT_IMPLEMENTATION",
                        "ledger_id": item.get("ledger_id"),
                        "target_path": target,
                        "blocking": True,
                    }
                )
                continue
            target_count += 1
            lowered = target.lower()
            if (
                lowered.startswith("vendor/")
                or lowered.startswith("vendor-runtimes/")
                or lowered.startswith("source-pool/")
                or lowered.startswith("runtime-sources/")
            ):
                findings.append(
                    {
                        "code": "TARGET_IS_SOURCE_POOL",
                        "ledger_id": item.get("ledger_id"),
                        "target_path": target,
                        "blocking": True,
                    }
                )
    expected_owner = "typescript.CanonicalProjectionStore"
    if canonical_projection_owners != {expected_owner}:
        findings.append(
            {
                "code": "CANONICAL_PROJECTION_OWNER_CONFLICT",
                "owners": sorted(canonical_projection_owners),
                "expected": expected_owner,
                "blocking": True,
            }
        )
    blocking = [item for item in findings if item["blocking"]]
    return {
        "schema": "zyra.m2-01-source-to-target-audit/v1",
        "implementation_commit": implementation_commit,
        "owner_units": sorted(OWNERS),
        "entry_count": len(entries),
        "production_entry_count": production_entries,
        "conformance_entry_count": conformance_entries,
        "resolved_source_file_count": source_file_count,
        "resolved_target_count": target_count,
        "canonical_projection_owners": sorted(canonical_projection_owners),
        "findings": findings,
        "blocking_findings": len(blocking),
        "passed": not blocking,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    parser.add_argument(
        "--implementation-commit",
        default="HEAD",
        help="Committed Zyra target to validate (default: HEAD).",
    )
    args = parser.parse_args()
    result = audit(args.implementation_commit)
    if args.summary:
        for key in (
            "implementation_commit",
            "entry_count",
            "production_entry_count",
            "conformance_entry_count",
            "resolved_source_file_count",
            "resolved_target_count",
            "blocking_findings",
            "passed",
        ):
            print(f"{key}={result[key]}")
        for finding in result["findings"]:
            print(
                "finding="
                + json.dumps(finding, ensure_ascii=False, sort_keys=True)
            )
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
