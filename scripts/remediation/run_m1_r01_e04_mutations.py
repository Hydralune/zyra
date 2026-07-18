"""Reversible Execution 04 mutation operators.

G0 identities and purposes remain immutable in the root manifest. Operators
are enabled slice-by-slice after their migrated target and exact killer test
exist. Backups live under Zyra's ignored .tmp boundary and restores refuse to
overwrite a target that no longer matches the applied mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ZYRA_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = (
    ZYRA_ROOT.parent
    / "docs"
    / "remediations"
    / "M1-R01-claude-source-custody"
    / "manifests"
    / "execution-04-mutation-manifest.jsonl"
)
BACKUP_ROOT = ZYRA_ROOT / ".tmp" / "e04-mutations"


OPERATORS: dict[str, dict[str, str]] = {
    "e04-mutation-domain-01": {
        "target": "packages/runtime/claude-runtime/src/query/lifecycle-runtime.ts",
        "needle": (
            "  ask(input: QueryAskInput): QueryAdmission {\n"
            "    if (process.env.ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME === \"1\") {"
        ),
        "replacement": (
            "  ask(input: QueryAskInput): QueryAdmission {\n"
            "    if (process.env.ZYRA_E04_MUTATION_DOMAIN_01 !== \"disabled\") { // E04 mutation: disconnect query source owner"
        ),
    },
    "e04-mutation-domain-02": {
        "target": "packages/runtime/claude-runtime/src/compact/compaction-custody-runtime.ts",
        "needle": (
            "  assertSourceRuntimeEnabled(): void {\n"
            "    if (process.env.ZYRA_DISABLE_E04_COMPACT_SOURCE_RUNTIME === \"1\") {"
        ),
        "replacement": (
            "  assertSourceRuntimeEnabled(): void {\n"
            "    if (process.env.ZYRA_E04_MUTATION_DOMAIN_02 !== \"disabled\") { // E04 mutation: disconnect compact source owner"
        ),
    },
    "e04-mutation-domain-03": {
        "target": "packages/runtime/claude-runtime/src/tools/execution-runtime.ts",
        "needle": (
            "  runTools(turnId: string, callIds: readonly string[], maximumConcurrency = 10): ToolBatch[] {\n"
            "    if (process.env.ZYRA_DISABLE_E04_TOOL_SOURCE_RUNTIME === \"1\") {"
        ),
        "replacement": (
            "  runTools(turnId: string, callIds: readonly string[], maximumConcurrency = 10): ToolBatch[] {\n"
            "    if (process.env.ZYRA_E04_MUTATION_DOMAIN_03 !== \"disabled\") { // E04 mutation: disconnect tool source owner"
        ),
    },
    "e04-mutation-domain-04": {
        "target": "packages/runtime/claude-runtime/src/permission/hook-runtime.ts",
        "needle": (
            "export function assertPermissionSourceRuntimeEnabled(): void {\n"
            "  if (process.env.ZYRA_DISABLE_E04_PERMISSION_SOURCE_RUNTIME === \"1\") {"
        ),
        "replacement": (
            "export function assertPermissionSourceRuntimeEnabled(): void {\n"
            "  if (process.env.ZYRA_E04_MUTATION_DOMAIN_04 !== \"disabled\") { // E04 mutation: disconnect permission source owner"
        ),
    },
    "e04-mutation-domain-05": {
        "target": "packages/integrations/claude-mcp/src/connection/connection-runtime.ts",
        "needle": (
            "export function assertMcpSourceRuntimeEnabled(): void {\n"
            "  if (process.env.ZYRA_DISABLE_E04_MCP_SOURCE_RUNTIME === \"1\") {"
        ),
        "replacement": (
            "export function assertMcpSourceRuntimeEnabled(): void {\n"
            "  if (process.env.ZYRA_E04_MUTATION_DOMAIN_05 !== \"disabled\") { // E04 mutation: disconnect MCP source owner"
        ),
    },
    "e04-mutation-domain-06": {
        "target": "packages/runtime/claude-runtime/src/skills/runtime.ts",
        "needle": (
            "  static assertSourceRuntimeEnabled(): void {\n"
            "    if (process.env.ZYRA_DISABLE_E04_SKILL_SOURCE_RUNTIME === \"1\") {"
        ),
        "replacement": (
            "  static assertSourceRuntimeEnabled(): void {\n"
            "    if (process.env.ZYRA_E04_MUTATION_DOMAIN_06 !== \"disabled\") { // E04 mutation: disconnect skill/plugin/command source owner"
        ),
    },
}


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def records() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def backup_path(record_id: str) -> Path:
    return BACKUP_ROOT / f"{record_id}.json"


def resolve_target(relative: str) -> Path:
    target = (ZYRA_ROOT / relative).resolve()
    try:
        target.relative_to(ZYRA_ROOT.resolve())
    except ValueError as error:
        raise SystemExit(f"mutation target escapes Zyra root: {target}") from error
    return target


def apply_operator(record_id: str) -> dict[str, object]:
    operator = OPERATORS.get(record_id)
    if operator is None:
        raise SystemExit(f"E04 mutation operator is not implemented in the current slice: {record_id}")
    target = resolve_target(operator["target"])
    current = target.read_bytes()
    current_text = current.decode("utf-8")
    backup = backup_path(record_id)
    if backup.exists():
        state = json.loads(backup.read_text(encoding="utf-8"))
        if sha256(current) != state["mutated_sha256"]:
            raise SystemExit(f"applied mutation target drifted; restore refused: {record_id}")
        return {
            "record_id": record_id,
            "status": "already_applied",
            "target": operator["target"],
            "mutated_sha256": state["mutated_sha256"],
        }
    if current_text.count(operator["needle"]) != 1:
        raise SystemExit(f"mutation anchor is not unique in {operator['target']}: {record_id}")
    mutated_text = current_text.replace(
        operator["needle"],
        operator["replacement"],
        1,
    )
    mutated = mutated_text.encode("utf-8")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    state = {
        "record_id": record_id,
        "target": operator["target"],
        "original_sha256": sha256(current),
        "mutated_sha256": sha256(mutated),
        "original_content": current_text,
    }
    backup.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    target.write_bytes(mutated)
    return {
        "record_id": record_id,
        "status": "applied",
        "target": operator["target"],
        "original_sha256": state["original_sha256"],
        "mutated_sha256": state["mutated_sha256"],
    }


def restore_operator(record_id: str) -> dict[str, object]:
    operator = OPERATORS.get(record_id)
    if operator is None:
        raise SystemExit(f"E04 mutation operator is not implemented in the current slice: {record_id}")
    backup = backup_path(record_id)
    if not backup.exists():
        return {
            "record_id": record_id,
            "status": "not_applied",
            "target": operator["target"],
        }
    state = json.loads(backup.read_text(encoding="utf-8"))
    target = resolve_target(str(state["target"]))
    current = target.read_bytes()
    if sha256(current) != state["mutated_sha256"]:
        raise SystemExit(f"mutation target drifted; restore refused: {record_id}")
    original = str(state["original_content"]).encode("utf-8")
    if sha256(original) != state["original_sha256"]:
        raise SystemExit(f"mutation backup checksum mismatch: {record_id}")
    target.write_bytes(original)
    backup.unlink()
    return {
        "record_id": record_id,
        "status": "restored",
        "target": state["target"],
        "restored_sha256": state["original_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--all", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--id")
    group.add_argument("--restore")
    args = parser.parse_args()
    corpus = records()
    known = {str(row["record_id"]) for row in corpus}
    if args.list:
        print(json.dumps({
            "records": corpus,
            "implemented_operator_ids": sorted(OPERATORS),
        }, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.all:
        pending = sorted(known - set(OPERATORS))
        if pending:
            raise SystemExit(
                "E04 --all is unavailable until later slices implement: "
                + ", ".join(pending)
            )
        raise SystemExit("E04 --all orchestration requires the final E04-F candidate gate")
    requested = args.id or args.restore
    if not requested:
        parser.error("one of --list, --all, --id or --restore is required")
    if requested not in known:
        raise SystemExit(f"unknown E04 mutation: {requested}")
    result = apply_operator(requested) if args.id else restore_operator(requested)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
