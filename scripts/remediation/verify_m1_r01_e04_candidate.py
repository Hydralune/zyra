"""Verify an E04 implementation candidate and emit candidate-bound evidence."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO.parent
MANIFEST_ROOT = WORKSPACE / "docs" / "remediations" / "M1-R01-claude-source-custody" / "manifests"
DEFAULT_OUTPUT = REPO / "docs" / "reviews" / "evidence" / "M1-R01-v4" / "execution-04"
BASELINE = "299b708d3559da7a5da1f9d6d55d2d1f1b155249"
SCHEMA = "4.0"
BUN = REPO / "node_modules" / "bun" / "bin" / "bun.exe"
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
SOURCE_REPOS = {
    "claude-code-best": WORKSPACE / "claude-code-best",
    "opencode": WORKSPACE / "opencode",
    "OpenClaw": WORKSPACE / "OpenClaw",
}
PYTHON_OWNER_REQUIRED_FIELDS = {
    "path", "symbol", "start_line", "end_line", "sha256",
    "responsibility_domain", "disposition", "default_reachable",
    "can_advance_logical_state", "can_select_policy_or_route",
    "can_fallback_for_typescript", "allowed_physical_durable_responsibility",
    "tests", "callsites", "callsite_scan_complete",
}
ALLOWED_CANDIDATE_PORT_ADDITIONS = {
    ("packages/workers/zyra_workers/typescript_claude_runtime.py", "_complete_result"),
    ("packages/workers/zyra_workers/typescript_claude_runtime.py", "_persist_terminal_receipt"),
    ("packages/workers/zyra_workers/typescript_claude_runtime.py", "_permission_queue"),
    ("packages/workers/zyra_workers/typescript_claude_runtime.py", "_find_e02_snapshot"),
    ("packages/workers/zyra_workers/typescript_claude_runtime.py", "_host_permission_responses"),
    ("packages/workers/zyra_workers/typescript_claude_runtime.py", "_project_permission_request"),
}


def run(
    command: list[str],
    *,
    cwd: Path = REPO,
    timeout: int = 600,
    check: bool = False,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    process = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "NO_COLOR": "1"},
        check=False,
    )
    result = {
        "command": command,
        "exit_code": process.returncode,
        "duration_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 3),
        "stdout_tail": process.stdout[-262144:],
        "stderr_tail": process.stderr[-262144:],
        "passed": process.returncode == 0,
    }
    if check and process.returncode:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def git(*args: str, cwd: Path = REPO) -> str:
    result = run(["git", *args], cwd=cwd, check=True)
    return result["stdout_tail"].strip()


def git_full(*args: str, cwd: Path = REPO) -> str:
    process = subprocess.run(
        ["git", *args], cwd=cwd, text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=True,
    )
    return process.stdout.strip()


def git_blob(candidate: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{candidate}:{path}"], cwd=REPO, capture_output=True, check=True,
    ).stdout


def sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )


def binding(candidate: str, tree: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "execution_id": "E04",
        "candidate_commit": candidate,
        "candidate_tree": tree,
        "verified_zyra_head": BASELINE,
        "g0_tooling_head": json_file(
            MANIFEST_ROOT / "execution-04-baseline-receipt.json"
        )["verified_zyra_head"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "toolchain": {
            "bun": run([str(BUN), "--version"])["stdout_tail"].strip(),
            "node": run(["node", "--version"])["stdout_tail"].strip(),
            "python": run([str(PYTHON), "--version"])["stdout_tail"].strip(),
        },
    }


def parse_json_output(result: dict[str, Any]) -> dict[str, Any] | None:
    text = result["stdout_tail"].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0:
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                return None
    return None


def target_rows(candidate: str, tree: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources = jsonl(MANIFEST_ROOT / "execution-04-source-recovery-manifest.jsonl")
    targets = jsonl(MANIFEST_ROOT / "execution-04-target-provenance-map.jsonl")
    source_by_id = {row["record_id"]: row for row in sources}
    reports: list[dict[str, Any]] = []
    similarity_rows: list[dict[str, Any]] = []
    for target in targets:
        blob = git_blob(candidate, target["target_path"])
        text = blob.decode("utf-8", errors="replace")
        lines = text.replace("\r", "").splitlines()
        anchors = []
        for anchor in target["retained_control_flow_anchors"]:
            start = max(1, int(anchor["target_start_line"]))
            end = min(len(lines), int(anchor["target_end_line"]))
            selected = "\n".join(lines[start - 1 : end])
            anchors.append({
                **anchor,
                "candidate_target_fingerprint": sha256(selected),
                "candidate_start_line": start,
                "candidate_end_line": end,
                "nonempty": bool(selected.strip()),
            })
        symbol_leaf = str(target["target_symbol"]).split(".")[-1]
        source = source_by_id[target["source_record_id"]]
        source_blob = subprocess.run(
            ["git", "show", f"{source['source_snapshot']}:{source['source_path']}"],
            cwd=SOURCE_REPOS[source["source_repo"]], capture_output=True, check=True,
        ).stdout.decode("utf-8", errors="replace")
        source_lines = source_blob.replace("\r", "").splitlines()
        source_selected = "\n".join(source_lines[source["start_line"] - 1 : source["end_line"]])
        target_tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower()))
        source_tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", source_selected.lower()))
        overlap = len(target_tokens & source_tokens) / max(1, len(source_tokens))
        report = {
            **binding(candidate, tree),
            "record_id": target["record_id"],
            "source_record_id": target["source_record_id"],
            "semantic_domain": source["semantic_domain"],
            "target_path": target["target_path"],
            "target_symbol": target["target_symbol"],
            "candidate_sha256": sha256(blob),
            "candidate_symbol_present": symbol_leaf in text,
            "default_entry_id": target["default_entry_id"],
            "default_entry_edges": target["default_entry_edges"],
            "state_store": target["state_store"],
            "state_effect_kind": target["state_effect_kind"],
            "anchors": anchors,
            "source_token_coverage_diagnostic": round(overlap, 6),
            "passed": symbol_leaf in text and all(item["nonempty"] for item in anchors),
        }
        reports.append(report)
        similarity_rows.append({
            "record_id": target["record_id"],
            "source_record_id": source["record_id"],
            "diagnostic_only": True,
            "source_token_coverage": round(overlap, 6),
        })
    return reports, similarity_rows


def line_buckets(candidate: str, tree: str) -> dict[str, Any]:
    output = git_full("diff", "--numstat", BASELINE, candidate, "--")
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"added": 0, "deleted": 0, "files": []})
    for line in output.splitlines():
        added_text, deleted_text, path = line.split("\t", 2)
        added = int(added_text) if added_text.isdigit() else 0
        deleted = int(deleted_text) if deleted_text.isdigit() else 0
        lowered = path.lower()
        if path.startswith("docs/"):
            bucket = "docs"
        elif path.startswith("dist/") or "/generated/" in lowered:
            bucket = "generated"
        elif path.startswith(("vendor/", "vendor-runtimes/", "source-pool/", "runtime-sources/")):
            bucket = "vendor-like/source-pool"
        elif path.startswith("tests/") or "/test/" in lowered or lowered.endswith((".test.ts", "_test.py")):
            bucket = "test"
        elif path.startswith("scripts/remediation/"):
            bucket = "audit-tooling"
        elif lowered.endswith((".json", ".jsonl", ".yaml", ".yml", ".csv")):
            bucket = "data"
        elif path in {
            "packages/workers/zyra_workers/typescript_claude_runtime.py",
            "packages/runtime/zyra_runtime/runtime_events/typescript_port.py",
        }:
            bucket = "adapter-only"
        else:
            bucket = "production"
        buckets[bucket]["added"] += added
        buckets[bucket]["deleted"] += deleted
        buckets[bucket]["files"].append(path)
    return {
        **binding(candidate, tree),
        "record_type": "line_buckets",
        "baseline_commit": BASELINE,
        "buckets": dict(sorted(buckets.items())),
        "total_added": sum(value["added"] for value in buckets.values()),
        "total_deleted": sum(value["deleted"] for value in buckets.values()),
        "note": "E04 has no independent line minimum; buckets prevent generated/data/vendor-like/adapter-only inflation.",
    }


def dependency_audit(candidate: str, tree: str, profile: dict[str, Any]) -> dict[str, Any]:
    tracked = git_full("ls-tree", "-r", "--name-only", candidate).splitlines()
    descriptors = {
        path for path in tracked
        if Path(path).name in {"package.json", "bun.lock", "pyproject.toml", "Dockerfile"}
        or Path(path).name.startswith("requirements")
    }
    production = {
        path for path in tracked
        if path.startswith(("apps/", "packages/"))
        and "/test/" not in path.lower()
        and not path.lower().endswith((".test.ts", "_test.py"))
        and Path(path).suffix.lower() in {".ts", ".tsx", ".js", ".mjs", ".py"}
    }
    hits: list[dict[str, Any]] = []
    ignored_mentions: list[dict[str, Any]] = []
    dependency_syntax = re.compile(
        r"^\s*(?:from\s+\S+\s+import\s+|import\s+\S+)"
        r"|\b(?:require|spawn|exec)\s*\("
        r"|[\"'](?:dependencies|devDependencies)[\"']\s*:"
        r"|^\s*(?:FROM|RUN)\s+",
        re.I,
    )
    for path in sorted(descriptors | production):
        text = git_blob(candidate, path).decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for pattern in profile["forbidden_dependency_patterns"]:
                if pattern.lower() not in line.lower():
                    continue
                entry = {"path": path, "line": number, "pattern": pattern, "text": line.strip()[:300]}
                if path in descriptors or dependency_syntax.search(line):
                    hits.append(entry)
                else:
                    ignored_mentions.append(entry)
    return {
        **binding(candidate, tree),
        "record_type": "dependency_audit",
        "scanned_descriptor_count": len(descriptors),
        "scanned_production_file_count": len(production),
        "forbidden_hits": hits,
        "ignored_non_dependency_mentions": ignored_mentions,
        "forbidden_dependency_count": len(hits),
        "passed": not hits,
    }


def python_owner_report(candidate: str, tree: str) -> dict[str, Any]:
    owners = jsonl(MANIFEST_ROOT / "execution-04-python-owner-census.jsonl")
    schema_errors: list[dict[str, Any]] = []
    for row in owners:
        missing_fields = sorted(PYTHON_OWNER_REQUIRED_FIELDS - set(row))
        if missing_fields:
            schema_errors.append({"record_id": row.get("record_id"), "missing_fields": missing_fields})
            continue
        if not isinstance(row["tests"], list) or not isinstance(row["callsites"], list):
            schema_errors.append({"record_id": row.get("record_id"), "invalid_fields": ["tests", "callsites"]})
        if row["callsite_scan_complete"] is not True:
            schema_errors.append({"record_id": row.get("record_id"), "invalid_fields": ["callsite_scan_complete"]})
    logical = [row for row in owners if row["can_advance_logical_state"] or row["can_select_policy_or_route"] or row["can_fallback_for_typescript"]]
    paths = sorted({row["path"] for row in owners})
    missing = []
    candidate_hashes = {}
    for path in paths:
        try:
            candidate_hashes[path] = sha256(git_blob(candidate, path))
        except subprocess.CalledProcessError:
            missing.append(path)
    baseline_symbols = {(row["path"], row["symbol"]) for row in owners}
    candidate_symbols: set[tuple[str, str]] = set()
    candidate_symbol_records: list[dict[str, Any]] = []
    for path in paths:
        try:
            parsed = ast.parse(git_blob(candidate, path).decode("utf-8"), filename=path)
        except (subprocess.CalledProcessError, SyntaxError):
            continue
        for node in ast.walk(parsed):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            key = (path, node.name)
            candidate_symbols.add(key)
            candidate_symbol_records.append({
                "path": path,
                "symbol": node.name,
                "start_line": int(node.lineno),
                "end_line": int(getattr(node, "end_lineno", node.lineno)),
            })
    candidate_additions = sorted(candidate_symbols - baseline_symbols)
    unexpected_additions = [key for key in candidate_additions if key not in ALLOWED_CANDIDATE_PORT_ADDITIONS]
    missing_retained_symbols = sorted(baseline_symbols - candidate_symbols)
    return {
        **binding(candidate, tree),
        "record_type": "python_owner_result",
        "census_symbols": len(owners),
        "census_paths": len(paths),
        "candidate_path_hashes": candidate_hashes,
        "missing_candidate_paths": missing,
        "schema_errors": schema_errors,
        "candidate_symbol_records": candidate_symbol_records,
        "candidate_owner_additions": [
            {"path": path, "symbol": symbol, "allowed_port_addition": (path, symbol) in ALLOWED_CANDIDATE_PORT_ADDITIONS}
            for path, symbol in candidate_additions
        ],
        "unexpected_candidate_owner_additions": [
            {"path": path, "symbol": symbol} for path, symbol in unexpected_additions
        ],
        "missing_retained_symbols": [
            {"path": path, "symbol": symbol} for path, symbol in missing_retained_symbols
        ],
        "python_logical_owner_records": [row["record_id"] for row in logical],
        "python_logical_owner_count": len(logical),
        "python_fallback": False,
        "passed": not missing and not logical and not schema_errors and not unexpected_additions and not missing_retained_symbols,
    }


def execute_evidence(candidate: str, tree: str, targets: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    e04_test = run([str(BUN), "test", "packages/runtime/claude-runtime/test/e04"], timeout=600)
    prebuild = run([str(BUN), "run", "build"], timeout=600)
    test_text = e04_test["stdout_tail"] + e04_test["stderr_tail"]
    domain_ids: dict[str, set[str]] = defaultdict(set)
    source_rows = {row["record_id"]: row for row in jsonl(MANIFEST_ROOT / "execution-04-source-recovery-manifest.jsonl")}
    baseline_targets = {row["record_id"]: row for row in jsonl(MANIFEST_ROOT / "execution-04-target-provenance-map.jsonl")}
    for report in targets:
        raw = baseline_targets[report["record_id"]]
        domain = source_rows[raw["source_record_id"]]["semantic_domain"]
        for field in ("success_test_ids", "failure_test_ids", "restore_test_ids", "disable_test_ids"):
            domain_ids[domain].update(raw[field])
    domain_closure = {
        domain: {
            "required_test_ids": sorted(ids),
            "observed_test_ids": sorted(test_id for test_id in ids if test_id in test_text),
            "passed": e04_test["passed"] and all(test_id in test_text for test_id in ids),
        }
        for domain, ids in sorted(domain_ids.items())
    }
    default_commands = [
        [str(BUN), "apps/code-worker/src/main.ts", "--stdio-probe"],
        [str(BUN), "dist/code-worker/main.js", "--stdio-probe"],
        ["node", "dist/code-worker-node/main.js", "--stdio-probe"],
        [str(PYTHON), "scripts/remediation/probe_m1_r01_e04.py", "python-bridge"],
        [str(PYTHON), "scripts/remediation/probe_m1_r01_e04.py", "api-route"],
        [str(PYTHON), "scripts/remediation/probe_m1_r01_e04.py", "source-ports"],
        [str(PYTHON), "scripts/remediation/probe_m1_r01_e04.py", "built-ports"],
    ]
    default_results = []
    for command in default_commands:
        result = run(command, timeout=300)
        result["parsed"] = parse_json_output(result)
        default_results.append(result)
    default_report = {
        **binding(candidate, tree),
        "record_type": "default_path_result",
        "commands": default_results,
        "domain_closure": domain_closure,
        "semantic_domain_passes": sum(1 for value in domain_closure.values() if value["passed"]),
        "canonical_owner": "typescript",
        "python_fallback": False,
        "passed": all(result["passed"] for result in default_results) and all(value["passed"] for value in domain_closure.values()),
    }
    crash_results = []
    terminal_fault_modes = (
        "checkpoint-before-ack",
        "final-checkpoint-before-terminal",
        "lost-ack",
        "host-disconnect",
        "typescript-disconnect",
    )
    for mode in (
        *terminal_fault_modes,
        "duplicate-ack",
        "stale-writer",
        "corrupt-checkpoint",
        "disable",
    ):
        result = run([str(PYTHON), "scripts/remediation/probe_m1_r01_e04.py", mode], timeout=300)
        result["mode"] = mode
        result["parsed"] = parse_json_output(result)
        crash_results.append(result)
    crash_report = {
        **binding(candidate, tree),
        "record_type": "terminal_protocol_crash_matrix",
        "cases": crash_results,
        "terminal_fault_modes": list(terminal_fault_modes),
        "terminal_fault_points": sum(
            1
            for result in crash_results
            if result["mode"] in terminal_fault_modes
            and result["passed"]
            and bool((result["parsed"] or {}).get("ok"))
        ),
        "minimum_restart_epochs": min(
            [
                int(item.get("restart_epochs") or 0)
                for result in crash_results
                if result["mode"] in terminal_fault_modes
                for item in list((result["parsed"] or {}).get("results") or [])
                if result["mode"] != "lost-ack"
            ]
            or [0]
        ),
        "passed": all(
            result["passed"]
            and bool(
                (result["parsed"] or {}).get("passed")
                or (result["parsed"] or {}).get("ok")
            )
            for result in crash_results
        ) and all(
            any(
                int(item.get("real_process_kills") or 0) >= 1
                for item in list((result["parsed"] or {}).get("results") or [])
            )
            for result in crash_results
            if result["mode"] in terminal_fault_modes
        ),
    }
    build_commands = [
        [str(BUN), "run", "typecheck"],
        [str(BUN), "test", "packages/runtime/claude-runtime/test", "packages/integrations/claude-mcp/test"],
        [str(PYTHON), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", ".tmp/e04-candidate-pytest", "tests/integration/test_e04_candidate_closure.py", "tests/integration/test_e01_typescript_runtime_cutover.py", "tests/integration/test_code_worker_clean_productized_runtime.py", "tests/integration/test_workspace_worker_gateway.py"],
    ]
    build_results = [e04_test, prebuild]
    for command in build_commands:
        build_results.append(run(command, timeout=600))
    build_report = {
        **binding(candidate, tree),
        "record_type": "build_and_test_result",
        "commands": build_results,
        "passed": all(result["passed"] for result in build_results),
    }
    return default_report, crash_report, build_report


def existing_report(path: Path, candidate: str) -> tuple[dict[str, Any] | None, bool]:
    if not path.is_file():
        return None, False
    value = json_file(path)
    bound = value.get("candidate_commit") == candidate or value.get("candidate") == candidate
    passed = bool(value.get("passed") or value.get("ok"))
    return value, bound and passed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--line-buckets", action="store_true")
    parser.add_argument("--similarity", action="store_true")
    args = parser.parse_args()
    candidate = args.candidate or git_full("rev-parse", "HEAD")
    tree = git_full("rev-parse", f"{candidate}^{{tree}}")
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    buckets = line_buckets(candidate, tree)
    write_json(output / "line-buckets.json", buckets)
    if args.line_buckets:
        print(json.dumps(buckets, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    g0 = run([str(PYTHON), "scripts/remediation/verify_m1_r01_e04_g0.py"])
    baseline_receipt = json_file(MANIFEST_ROOT / "execution-04-baseline-receipt.json")
    write_json(output / "baseline-receipt.json", baseline_receipt)
    reports, similarity = target_rows(candidate, tree)
    write_jsonl(output / "target-provenance-report.jsonl", reports)
    sources = jsonl(MANIFEST_ROOT / "execution-04-source-recovery-manifest.jsonl")
    source_report = {
        **binding(candidate, tree),
        "record_type": "source_recovery_report",
        "g0_verification": g0,
        "source_range_count": len(sources),
        "target_record_count": len(reports),
        "semantic_domains": sorted({row["semantic_domain"] for row in sources}),
        "candidate_target_records_passed": sum(1 for row in reports if row["passed"]),
        "similarity_diagnostics": similarity,
        "passed": g0["passed"] and len(sources) == len(reports) == 18 and all(row["passed"] for row in reports),
    }
    write_json(output / "source-recovery-report.json", source_report)
    write_json(output / "reimplementation-exceptions.json", {
        **binding(candidate, tree), "record_type": "reimplementation_exceptions", "exceptions": [], "passed": True,
    })
    if args.similarity:
        print(json.dumps(similarity, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    profile = json_file(MANIFEST_ROOT / "execution-04-gate-profile.json")
    dependencies = dependency_audit(candidate, tree, profile)
    owners = python_owner_report(candidate, tree)
    write_json(output / "dependency-audit.json", dependencies)
    write_json(output / "python-owner-result.json", owners)
    default_report, crash_report, build_report = execute_evidence(candidate, tree, reports)
    write_json(output / "default-path-result.json", default_report)
    write_json(output / "terminal-protocol-crash-matrix.json", crash_report)
    write_json(output / "build-and-test-result.json", build_report)
    cleanroom, cleanroom_ok = existing_report(output / "cleanroom-result.json", candidate)
    mutations, mutation_ok = existing_report(output / "mutation-results.json", candidate)
    mutation_count = len((mutations or {}).get("results", []))
    required_mutation_count = len(jsonl(MANIFEST_ROOT / "execution-04-mutation-manifest.jsonl"))
    mutation_items = (mutations or {}).get("results", [])
    mutation_ok = mutation_ok and mutation_count == required_mutation_count and all(
        item.get("ok") is True and item.get("killer_result") == "killed"
        for item in mutation_items
    )
    thresholds = {
        "credited_domain_count": len(source_report["semantic_domains"]),
        "default_path_required_passes": default_report["semantic_domain_passes"],
        "dirty_cleanroom_path_count": len((cleanroom or {}).get("dirty_paths", ["missing"])),
        "forbidden_dependency_count": dependencies["forbidden_dependency_count"],
        "mutation_kill_rate": (
            sum(
                1
                for item in mutation_items
                if item.get("ok") is True and item.get("killer_result") == "killed"
            )
            / mutation_count
        ) if mutation_count else 0.0,
        "python_logical_owner_count": owners["python_logical_owner_count"],
        "terminal_fault_points": crash_report["terminal_fault_points"],
        "minimum_restart_epochs": crash_report["minimum_restart_epochs"],
    }
    gate_checks = {
        "g0_immutable": g0["passed"],
        "source_recovery": source_report["passed"],
        "target_provenance": all(row["passed"] for row in reports),
        "default_path": default_report["passed"] and thresholds["default_path_required_passes"] == 8,
        "terminal_crash_matrix": (
            crash_report["passed"]
            and thresholds["terminal_fault_points"] == profile["thresholds"]["terminal_fault_points"]
            and thresholds["minimum_restart_epochs"] >= profile["thresholds"]["minimum_restart_epochs"]
        ),
        "python_owner": owners["passed"] and thresholds["python_logical_owner_count"] == 0,
        "dependency_audit": dependencies["passed"],
        "build_and_test": build_report["passed"],
        "cleanroom": cleanroom_ok and thresholds["dirty_cleanroom_path_count"] == 0,
        "mutations": mutation_ok and thresholds["mutation_kill_rate"] == 1.0,
    }
    gate = {
        **binding(candidate, tree),
        "record_type": "candidate_gate_result",
        "baseline_commit": BASELINE,
        "gate_checks": gate_checks,
        "thresholds": thresholds,
        "required_thresholds": profile["thresholds"],
        "candidate_gate_passed": all(gate_checks.values()),
        "status_if_passed": "implementation_complete_review_pending",
        "independent_review_required": True,
        "passed": all(gate_checks.values()),
    }
    write_json(output / "candidate-gate-result.json", gate)
    print(json.dumps(gate, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
