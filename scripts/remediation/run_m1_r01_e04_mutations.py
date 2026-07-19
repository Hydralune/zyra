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
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


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


OPERATORS: dict[str, dict[str, Any]] = {
    "e04-mutation-terminal-order": {
        "target": "packages/runtime/claude-runtime/src/stdio.ts",
        "edits": (
            {
                "needle": (
                    "    capabilities = null;\n"
                    "    await activeCapabilities.close();\n"
                    "    terminalResultSent = true;"
                ),
                "replacement": (
                    "    capabilities = null;\n"
                    "    terminalResultSent = true;"
                ),
            },
            {
                "needle": "    } as unknown as JsonObject);",
                "replacement": (
                    "    } as unknown as JsonObject);\n"
                    "    await activeCapabilities.close(); // E04 mutation: checkpoint after terminal close"
                ),
            },
        ),
    },
    "e04-mutation-checkpoint-ack-loss": {
        "target": "packages/runtime/claude-runtime/src/stdio.ts",
        "needle": (
            "    const frame = await this.read(\"runtime.checkpoint.result\", correlationId);\n"
            "    if (frame.payload.accepted !== true) {"
        ),
        "replacement": (
            "    const frame = await this.read(\"runtime.checkpoint.result\", correlationId);\n"
            "    if (frame.payload.accepted === true) { // E04 mutation: discard a valid checkpoint ACK"
        ),
    },
    "e04-mutation-checkpoint-ack-duplicate": {
        "target": "packages/runtime/claude-runtime/src/stdio.ts",
        "needle": (
            "    this.send(\"run.result\", {\n"
            "      result,\n"
            "      terminal_id: terminalId,\n"
            "      terminal_revision: 1,\n"
            "      requires_ack: true,\n"
            "    }, terminalId);\n"
            "    const acknowledgement = await this.read(\"run.result.ack\", terminalId);"
        ),
        "replacement": (
            "    this.send(\"run.result\", {\n"
            "      result,\n"
            "      terminal_id: terminalId,\n"
            "      terminal_revision: 1,\n"
            "      requires_ack: true,\n"
            "    }, terminalId);\n"
            "    this.send(\"run.result\", { // E04 mutation: duplicate terminal delivery before ACK\n"
            "      result,\n"
            "      terminal_id: terminalId,\n"
            "      terminal_revision: 1,\n"
            "      requires_ack: true,\n"
            "    }, terminalId);\n"
            "    const acknowledgement = await this.read(\"run.result.ack\", terminalId);"
        ),
    },
    "e04-mutation-python-host-disconnect": {
        "target": "packages/runtime/claude-runtime/src/stdio.ts",
        "needle": (
            "  private async read(kind: RuntimeFrameKind, correlationId: string): Promise<RuntimeFrame> {\n"
            "    const selected = await this.lines.next();\n"
            "    if (selected.done || typeof selected.value !== \"string\") {"
        ),
        "replacement": (
            "  private async read(kind: RuntimeFrameKind, correlationId: string): Promise<RuntimeFrame> {\n"
            "    const selected = await this.lines.next();\n"
            "    if (!selected.done && typeof selected.value === \"string\") {\n"
            "      this.aborted = true; // E04 mutation: disconnect the Python host transport\n"
            "      throw new RuntimeProtocolError(\"host_disconnected\", \"mutated Python host disconnect\");\n"
            "    }\n"
            "    if (selected.done || typeof selected.value !== \"string\") {"
        ),
    },
    "e04-mutation-typescript-disconnect": {
        "target": "packages/runtime/claude-runtime/src/stdio.ts",
        "needle": (
            "    capabilities = null;\n"
            "    await activeCapabilities.close();\n"
            "    terminalResultSent = true;"
        ),
        "replacement": (
            "    capabilities = null;\n"
            "    await activeCapabilities.close();\n"
            "    process.exit(86); // E04 mutation: kill TypeScript owner before terminal delivery\n"
            "    terminalResultSent = true;"
        ),
    },
    "e04-mutation-python-fallback": {
        "target": "packages/workers/zyra_workers/typescript_claude_runtime.py",
        "needle": (
            "        return ClaudeQueryEngineResult(\n"
            "            ok=False,"
        ),
        "replacement": (
            "        return ClaudeQueryEngineResult(\n"
            "            ok=True,  # E04 mutation: pretend Python completed after TypeScript failure"
        ),
    },
    "e04-mutation-root-dependency": {
        "target": "packages/workers/zyra_workers/typescript_claude_runtime.py",
        "needle": "        self.entrypoint = code_worker_entrypoint(self.project_root)",
        "replacement": (
            "        self.entrypoint = (self.project_root / \"../claude-code-best/src/entry.ts\").resolve()"
            "  # E04 mutation: root-source runtime dependency"
        ),
    },
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
    "e04-mutation-domain-07": {
        "target": "packages/runtime/claude-runtime/src/agents/run-agent.ts",
        "needle": (
            "  assertAgentSourceRuntimeEnabled();\n"
            "  const input = isE03Task(task)"
        ),
        "replacement": (
            "  if (process.env.ZYRA_E04_MUTATION_DOMAIN_07 !== \"disabled\") { // E04 mutation: disconnect AgentTool run/resume owner\n"
            "    throw new E03RuntimeError(\"agent_source_runtime_disconnected\", \"AgentTool run/resume source owner is disconnected\");\n"
            "  }\n"
            "  assertAgentSourceRuntimeEnabled();\n"
            "  const input = isE03Task(task)"
        ),
    },
    "e04-mutation-domain-08": {
        "target": "packages/runtime/claude-runtime/src/isolation/request-runtime.ts",
        "needle": (
            "  ): E03IsolationRequest {\n"
            "    this.assertSourceRuntimeEnabled();"
        ),
        "replacement": (
            "  ): E03IsolationRequest {\n"
            "    if (process.env.ZYRA_E04_MUTATION_DOMAIN_08 !== \"disabled\") { // E04 mutation: disconnect isolation/control source owner\n"
            "      throw new E03RuntimeError(\"isolation_source_runtime_disconnected\", \"worktree/isolation source owner is disconnected\");\n"
            "    }\n"
            "    this.assertSourceRuntimeEnabled();"
        ),
    },
}


KILLERS: dict[str, tuple[str, ...]] = {
    "e04-mutation-terminal-order": (
        "bun",
        "apps/code-worker/src/main.ts",
        "--stdio-probe",
    ),
    "e04-mutation-checkpoint-ack-loss": (
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/integration/test_e01_typescript_runtime_cutover.py::test_lost_terminal_ack_resumes_without_tool_reexecution",
    ),
    "e04-mutation-checkpoint-ack-duplicate": (
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/integration/test_e01_typescript_runtime_cutover.py::test_duplicate_terminal_delivery_is_acknowledged_idempotently",
    ),
    "e04-mutation-python-host-disconnect": (
        "python",
        "scripts/remediation/probe_m1_r01_e04.py",
        "host-disconnect",
    ),
    "e04-mutation-typescript-disconnect": (
        "python",
        "scripts/remediation/probe_m1_r01_e04.py",
        "typescript-disconnect",
    ),
    "e04-mutation-python-fallback": (
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/integration/test_e01_typescript_runtime_cutover.py::test_environment_disconnect_fails_without_python_fallback",
    ),
    "e04-mutation-root-dependency": (
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/integration/test_e04_candidate_closure.py::test_e04_root_dependency",
    ),
    "e04-mutation-domain-01": (
        "bun", "test", "packages/runtime/claude-runtime/test/e04/runtime-core-source-recovery.behavior.test.ts",
        "--test-name-pattern", "e04-query-disable",
    ),
    "e04-mutation-domain-02": (
        "bun", "test", "packages/runtime/claude-runtime/test/e04/runtime-core-source-recovery.behavior.test.ts",
        "--test-name-pattern", "e04-compact-disable",
    ),
    "e04-mutation-domain-03": (
        "bun", "test", "packages/runtime/claude-runtime/test/e04/runtime-core-source-recovery.behavior.test.ts",
        "--test-name-pattern", "e04-tool-disable",
    ),
    "e04-mutation-domain-04": (
        "bun", "test", "packages/runtime/claude-runtime/test/e04/permission-mcp-source-recovery.behavior.test.ts",
        "--test-name-pattern", "e04-permission-disable",
    ),
    "e04-mutation-domain-05": (
        "bun", "test", "packages/runtime/claude-runtime/test/e04/permission-mcp-source-recovery.behavior.test.ts",
        "--test-name-pattern", "e04-mcp-disable",
    ),
    "e04-mutation-domain-06": (
        "bun", "test", "packages/runtime/claude-runtime/test/e04/skill-plugin-command-source-recovery.behavior.test.ts",
        "--test-name-pattern", "e04-skill-disable",
    ),
    "e04-mutation-domain-07": (
        "bun", "test", "packages/runtime/claude-runtime/test/e04/agent-control-source-recovery.behavior.test.ts",
        "--test-name-pattern", "e04-agent-disable",
    ),
    "e04-mutation-domain-08": (
        "bun", "test", "packages/runtime/claude-runtime/test/e04/agent-control-source-recovery.behavior.test.ts",
        "--test-name-pattern", "e04-isolation-disable",
    ),
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
    edits = operator.get("edits") or (
        {"needle": operator["needle"], "replacement": operator["replacement"]},
    )
    mutated_text = current_text
    for edit in edits:
        needle = str(edit["needle"])
        replacement = str(edit["replacement"])
        if mutated_text.count(needle) != 1:
            raise SystemExit(
                f"mutation anchor is not unique in {operator['target']}: {record_id}"
            )
        mutated_text = mutated_text.replace(needle, replacement, 1)
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


def _bun_executable() -> str:
    configured = os.environ.get("ZYRA_BUN_EXECUTABLE", "").strip()
    local = ZYRA_ROOT / "node_modules" / "bun" / "bin" / (
        "bun.exe" if os.name == "nt" else "bun"
    )
    selected = configured or shutil.which("bun") or (str(local) if local.is_file() else "")
    if not selected:
        raise SystemExit("Bun 1.2.15 is required for E04 mutation orchestration")
    return selected


def _materialize_command(parts: tuple[str, ...]) -> list[str]:
    command = list(parts)
    if command[0] == "python":
        command[0] = sys.executable
        if command[1:3] == ["-m", "pytest"]:
            command[3:3] = [
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(
                    BACKUP_ROOT
                    / (
                        "pytest-"
                        + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:12]
                    )
                ),
            ]
    elif command[0] == "bun":
        command[0] = _bun_executable()
    return command


def _run(command: list[str], *, timeout: int = 300) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ZYRA_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    output = completed.stdout or ""
    return {
        "command": command,
        "returncode": completed.returncode,
        "output_tail": output[-4000:],
    }


def _compile_mutation(record_id: str) -> dict[str, Any]:
    target = OPERATORS[record_id]["target"]
    if target.endswith(".py"):
        return _run([sys.executable, "-m", "py_compile", target])
    return _run([_bun_executable(), "run", "typecheck:e02"])


def _git_value(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ZYRA_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def run_all_mutations() -> dict[str, Any]:
    corpus = records()
    record_by_id = {str(row["record_id"]): row for row in corpus}
    expected = set(record_by_id)
    if expected != set(OPERATORS) or expected != set(KILLERS):
        raise SystemExit(
            "E04 mutation corpus/operator/killer mismatch: "
            + json.dumps(
                {
                    "missing_operators": sorted(expected - set(OPERATORS)),
                    "extra_operators": sorted(set(OPERATORS) - expected),
                    "missing_killers": sorted(expected - set(KILLERS)),
                    "extra_killers": sorted(set(KILLERS) - expected),
                },
                sort_keys=True,
            )
        )
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for record in corpus:
        record_id = str(record["record_id"])
        target = resolve_target(OPERATORS[record_id]["target"])
        original_sha256 = sha256(target.read_bytes())
        applied: dict[str, object] | None = None
        compile_result: dict[str, Any] | None = None
        killer_result: dict[str, Any] | None = None
        restored: dict[str, object] | None = None
        baseline_result: dict[str, Any] | None = None
        failure = ""
        try:
            applied = apply_operator(record_id)
            compile_result = _compile_mutation(record_id)
            if compile_result["returncode"] != 0:
                failure = "mutation_did_not_compile"
            else:
                killer_result = _run(_materialize_command(KILLERS[record_id]))
                if killer_result["returncode"] == 0:
                    failure = "exact_killer_survived"
        except Exception as error:  # restoration still has priority
            failure = f"orchestration_error:{type(error).__name__}:{error}"
        finally:
            try:
                restored = restore_operator(record_id)
            except Exception as error:
                failure = f"restore_error:{type(error).__name__}:{error}"
        restored_sha256 = sha256(target.read_bytes())
        if restored_sha256 != original_sha256:
            failure = "restored_hash_mismatch"
        if not failure:
            baseline_result = _run(_materialize_command(KILLERS[record_id]))
            if baseline_result["returncode"] != 0:
                failure = "restored_exact_killer_failed"
        if failure:
            failures.append(f"{record_id}:{failure}")
        results.append(
            {
                "record_id": record_id,
                "family": record.get("family"),
                "target": OPERATORS[record_id]["target"],
                "exact_killer_test": record.get("exact_killer_test"),
                "original_sha256": original_sha256,
                "applied": applied,
                "compile": compile_result,
                "killer": killer_result,
                "killer_result": "killed" if killer_result and killer_result["returncode"] != 0 else "survived",
                "restore": restored,
                "restored_sha256": restored_sha256,
                "restored_killer": baseline_result,
                "ok": not failure,
                "failure": failure,
            }
        )
    residual_backups = list(BACKUP_ROOT.glob("*.json")) if BACKUP_ROOT.exists() else []
    if residual_backups:
        failures.append("residual_mutation_backups")
    return {
        "schema_version": "4.0",
        "execution_id": "E04",
        "candidate": _git_value("rev-parse", "HEAD"),
        "tree": _git_value("rev-parse", "HEAD^{tree}"),
        "g0_manifest": str(MANIFEST.relative_to(ZYRA_ROOT.parent)).replace("\\", "/"),
        "toolchain": {
            "python": sys.version.split()[0],
            "bun": _run([_bun_executable(), "--version"])["output_tail"].strip(),
        },
        "command": [sys.executable, "scripts/remediation/run_m1_r01_e04_mutations.py", "--all"],
        "ok": not failures,
        "killed": sum(1 for item in results if item["killer_result"] == "killed"),
        "total": len(results),
        "residual_backups": len(residual_backups),
        "failures": failures,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output")
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
        report = run_all_mutations()
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.output:
            output = resolve_target(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0 if report["ok"] else 1
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
