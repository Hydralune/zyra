from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".py": "python",
    ".rs": "rust",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
}
FORBIDDEN_PRODUCTION_PARTS = {
    "docs",
    "fixtures",
    "node_modules",
    "runtime-sources",
    "source-pool",
    "tests",
    "third_party",
    "vendor",
    "vendor-runtimes",
}


class CustodyViolation(ValueError):
    pass


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise CustodyViolation(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout


def _language(path: str) -> str:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.casefold(), "unknown")


def _validate_production_path(path: str) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise CustodyViolation(f"production path escapes repository: {path}")
    lowered = {part.casefold() for part in relative.parts}
    forbidden = sorted(lowered & FORBIDDEN_PRODUCTION_PARTS)
    if forbidden:
        raise CustodyViolation(
            f"production path {path} is in an excluded bucket: {', '.join(forbidden)}"
        )
    resolved = (ROOT / relative).resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as error:
        raise CustodyViolation(f"production path escapes repository: {path}") from error
    if not resolved.is_file():
        raise CustodyViolation(f"production path does not exist: {path}")
    if _language(path) == "unknown":
        raise CustodyViolation(f"production path has no auditable language mapping: {path}")
    return resolved


def _numstat_added(path: str, *, base: str, target: str) -> int:
    arguments = ["diff", "--numstat", base]
    if target != "WORKTREE":
        arguments.append(target)
    arguments.extend(("--", path))
    output = _git(*arguments)
    added = 0
    for line in output.splitlines():
        columns = line.split("\t", 2)
        if len(columns) < 3:
            continue
        try:
            added += int(columns[0])
        except ValueError:
            continue
    if target == "WORKTREE" and not output.strip():
        untracked = {
            item.strip().replace("\\", "/")
            for item in _git("ls-files", "--others", "--exclude-standard", "--", path).splitlines()
            if item.strip()
        }
        if path.replace("\\", "/") in untracked:
            added = sum(1 for _ in (ROOT / path).open(encoding="utf-8", errors="replace"))
    return added


def _load_ledger(path: str, *, target: str) -> Mapping[str, Any] | list[Any]:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise CustodyViolation(f"ledger path escapes repository: {path}")
    normalized = relative.as_posix()
    if target == "WORKTREE":
        raw = (ROOT / relative).read_text(encoding="utf-8")
    else:
        raw = _git("show", f"{target}:{normalized}")
    document = json.loads(raw)
    if not isinstance(document, (Mapping, list)):
        raise CustodyViolation("ledger root must be an object or list")
    return document


def _ledger_custody_violations(
    document: Mapping[str, Any],
    *,
    target: str,
) -> list[str]:
    config = document.get("ledger_custody")
    if config is None:
        return []
    if not isinstance(config, Mapping):
        return ["ledger_custody must be an object"]
    path = str(config.get("path") or "").strip()
    owner_unit = str(config.get("owner_unit") or document.get("slice_id") or "").strip()
    expected_count = int(config.get("expected_entry_count") or 0)
    if not path or not owner_unit or expected_count <= 0:
        return ["ledger_custody requires path, owner_unit and positive expected_entry_count"]
    ledger = _load_ledger(path, target=target)
    entries = ledger if isinstance(ledger, list) else ledger.get("entries")
    if not isinstance(entries, list):
        return ["ledger entries must be a list"]
    owned = [
        item
        for item in entries
        if isinstance(item, Mapping) and str(item.get("owner_unit") or "") == owner_unit
    ]
    violations: list[str] = []
    if len(owned) != expected_count:
        violations.append(
            f"ledger owner {owner_unit} has {len(owned)} entries, expected {expected_count}"
        )
    decisions = {
        (str(item.get("source_repo") or ""), str(item.get("role") or "")): item
        for item in list(document.get("source_decisions") or [])
        if isinstance(item, Mapping)
    }
    custody = {
        (str(item.get("source_repo") or ""), str(item.get("source_role") or "")): item
        for item in list(document.get("language_custody") or [])
        if isinstance(item, Mapping)
    }
    seen: set[tuple[str, str]] = set()
    for entry in owned:
        metadata = entry.get("metadata")
        if not isinstance(metadata, Mapping):
            violations.append(f"ledger {entry.get('ledger_id')} lacks metadata")
            continue
        key = (str(entry.get("source_repo") or ""), str(metadata.get("source_role") or ""))
        if key in seen:
            violations.append(f"ledger has duplicate source role {key[0]}/{key[1]}")
        seen.add(key)
        decision = decisions.get(key)
        policy = custody.get(key)
        if decision is None or policy is None:
            violations.append(f"ledger source role {key[0]}/{key[1]} lacks evidence policy")
            continue
        source_language = str(metadata.get("source_language") or "").casefold()
        target_language = str(metadata.get("target_language") or "").casefold()
        migration_mode = str(metadata.get("migration_mode") or "")
        expected = {
            str(item).casefold()
            for item in list(policy.get("expected_production_languages") or [])
        }
        if source_language != str(decision.get("source_language") or "").casefold():
            violations.append(f"{key[0]}/{key[1]} ledger source_language disagrees with evidence")
        if target_language != str(decision.get("target_language") or "").casefold():
            violations.append(f"{key[0]}/{key[1]} ledger target_language disagrees with evidence")
        if migration_mode != str(decision.get("migration_mode") or ""):
            violations.append(f"{key[0]}/{key[1]} ledger migration_mode disagrees with evidence")
        if target_language not in expected:
            violations.append(
                f"{key[0]}/{key[1]} ledger target_language {target_language} is outside policy"
            )
        if "same_language" in migration_mode.casefold() and source_language != target_language:
            violations.append(
                f"{key[0]}/{key[1]} same-language ledger is inverted: "
                f"{source_language}->{target_language}"
            )
    missing = sorted(set(custody) - seen)
    for source_repo, source_role in missing:
        violations.append(f"evidence source role {source_repo}/{source_role} is missing from ledger")
    return violations


def verify(document: Mapping[str, Any], *, base: str, target: str) -> dict[str, Any]:
    raw_entries = document.get("language_custody")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CustodyViolation("evidence must contain a non-empty language_custody list")
    reports: list[dict[str, Any]] = []
    violations: list[str] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            violations.append(f"language_custody[{index}] must be an object")
            continue
        source_repo = str(raw.get("source_repo") or "").strip()
        source_role = str(raw.get("source_role") or "").strip()
        migration_mode = str(raw.get("migration_mode") or "").strip()
        expected = {
            str(item).strip().casefold()
            for item in list(raw.get("expected_production_languages") or [])
            if str(item).strip()
        }
        paths = [str(item).replace("\\", "/") for item in list(raw.get("production_paths") or [])]
        minimums = {
            str(language).casefold(): int(count)
            for language, count in dict(raw.get("minimum_added_lines_by_language") or {}).items()
        }
        role_label = f"{source_repo or '<missing>'}/{source_role or '<missing>'}"
        if not source_repo or not source_role or not migration_mode:
            violations.append(f"{role_label}: source_repo, source_role and migration_mode are required")
        if not expected:
            violations.append(f"{role_label}: expected_production_languages is empty")
        if not paths:
            violations.append(f"{role_label}: production_paths is empty")
        additions: dict[str, int] = defaultdict(int)
        actual_languages: set[str] = set()
        for path in paths:
            try:
                _validate_production_path(path)
                language = _language(path)
                actual_languages.add(language)
                additions[language] += _numstat_added(path, base=base, target=target)
            except CustodyViolation as error:
                violations.append(f"{role_label}: {error}")
        unexpected = sorted(actual_languages - expected)
        if unexpected:
            violations.append(
                f"{role_label}: production paths use unexpected languages: {', '.join(unexpected)}"
            )
        for language, minimum in minimums.items():
            if language not in expected:
                violations.append(
                    f"{role_label}: minimum declared for non-expected language {language}"
                )
            actual = additions.get(language, 0)
            if actual < minimum:
                violations.append(
                    f"{role_label}: {language} added production lines {actual} < required {minimum}"
                )
        if raw.get("cross_language_exception") is True and not str(
            raw.get("cross_language_decision_id") or ""
        ).strip():
            violations.append(
                f"{role_label}: cross-language exception lacks cross_language_decision_id"
            )
        reports.append(
            {
                "source_repo": source_repo,
                "source_role": source_role,
                "migration_mode": migration_mode,
                "expected_production_languages": sorted(expected),
                "actual_production_languages": sorted(actual_languages),
                "added_production_lines": dict(sorted(additions.items())),
                "production_paths": paths,
            }
        )
    violations.extend(_ledger_custody_violations(document, target=target))
    return {
        "schema": "zyra.source-language-custody-report/v1",
        "base": base,
        "target": target,
        "ok": not violations,
        "roles": reports,
        "violations": violations,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed when a source role's required implementation language disappears."
    )
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--target", default="WORKTREE")
    arguments = parser.parse_args(argv)
    try:
        document = json.loads(arguments.evidence.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            raise CustodyViolation("evidence root must be an object")
        report = verify(document, base=arguments.base, target=arguments.target)
    except (OSError, json.JSONDecodeError, CustodyViolation, ValueError) as error:
        report = {
            "schema": "zyra.source-language-custody-report/v1",
            "base": arguments.base,
            "target": arguments.target,
            "ok": False,
            "roles": [],
            "violations": [f"{type(error).__name__}: {error}"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
