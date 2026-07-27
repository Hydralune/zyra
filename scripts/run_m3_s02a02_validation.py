from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SLICE_BASELINE = "c01fcacf7cd30c37aa9629aed76a7df5256dad63"
PRODUCTION_PATHS = (
    "apps/api/zyra_api/live_benchmark_port.py",
    "packages/evaluation/zyra_evaluation/live_benchmark/",
    "packages/evaluation/zyra_evaluation/scenario_runner/research_delivery.py",
)
FORBIDDEN_RUNTIME_REFERENCES = (
    "../claude-code-best",
    "../browser-use",
    "../OpenHands",
    "../opencode",
    "../openclaw",
    "G:\\agent-zoo\\claude-code-best",
    "G:\\agent-zoo\\browser-use",
    "G:\\agent-zoo\\OpenHands",
    "G:\\agent-zoo\\opencode",
    "G:\\agent-zoo\\openclaw",
)
DEPENDENCY_FILES = {
    "package.json",
    "bun.lock",
    "bun.lockb",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture exact-commit M3-S02A-02 validation receipts."
    )
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    return parser.parse_args()


def git(*arguments: str) -> str:
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
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run_command(
    command: list[str],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    combined = completed.stdout + (
        ("\n" + completed.stderr) if completed.stderr else ""
    )
    receipt = {
        "command": subprocess.list2cmdline(command),
        "exit_code": completed.returncode,
        "output_sha256": sha256_text(combined),
        "output_tail": combined[-4000:],
        "_output": combined,
    }
    if completed.returncode:
        raise RuntimeError(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


def test_count(output: str) -> int:
    matches = re.findall(r"(\d+)\s+passed", output)
    return sum(int(item) for item in matches)


def source_boundary(commit: str) -> dict[str, Any]:
    changed = tuple(
        line.strip().replace("\\", "/")
        for line in git(
            "diff",
            "--name-only",
            f"{SLICE_BASELINE}..{commit}",
        ).splitlines()
        if line.strip()
    )
    dependency_changes = sorted(
        path
        for path in changed
        if Path(path).name in DEPENDENCY_FILES
        and Path(path).name != "package.json"
    )
    if "package.json" in changed:
        baseline_package = json.loads(
            git("show", f"{SLICE_BASELINE}:package.json")
        )
        target_package = json.loads(git("show", f"{commit}:package.json"))
        dependency_keys = (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        )
        if any(
            baseline_package.get(key, {}) != target_package.get(key, {})
            for key in dependency_keys
        ):
            dependency_changes.append("package.json")
    production = [
        path
        for path in changed
        if any(
            path == prefix.rstrip("/") or path.startswith(prefix)
            for prefix in PRODUCTION_PATHS
        )
        and path.endswith(".py")
    ]
    forbidden: list[dict[str, str]] = []
    provider_calls: list[dict[str, str]] = []
    for path in production:
        content = git("show", f"{commit}:{path}")
        for marker in FORBIDDEN_RUNTIME_REFERENCES:
            if marker.casefold() in content.casefold():
                forbidden.append({"path": path, "marker": marker})
        for marker in (
            "provider_smoke_test.py",
            "openai.responses.create",
            "anthropic.messages.create",
            "/v1/responses\"",
            "/v1/messages\"",
        ):
            if marker.casefold() in content.casefold():
                provider_calls.append({"path": path, "marker": marker})
    findings = {
        "dependency_changes": dependency_changes,
        "forbidden_runtime_references": forbidden,
        "provider_model_call_sites": provider_calls,
    }
    if any(findings.values()):
        raise RuntimeError(
            "source boundary failed: "
            + json.dumps(findings, ensure_ascii=False, sort_keys=True)
        )
    projection = {
        "status": "passed",
        "changed_file_count": len(changed),
        "production_file_count": len(production),
        "new_external_dependencies": 0,
        "forbidden_root_runtime_paths": 0,
        "new_provider_model_call_sites": 0,
        "no_new_paid_model_api": True,
        "scan_digest": sha256_text(
            json.dumps(
                {"changed": changed, "production": production},
                ensure_ascii=False,
                sort_keys=True,
            )
        ),
    }
    return projection


def main() -> int:
    arguments = parse_args()
    commit = git("rev-parse", f"{arguments.commit}^{{commit}}").strip()
    if git("rev-parse", "HEAD").strip() != commit:
        raise RuntimeError("validation commit must be the current HEAD")
    python = sys.executable
    timeout = max(60.0, min(arguments.timeout_seconds, 1800.0))

    line = run_command(
        [
            python,
            "scripts/audit_m3_s02a02_effective_code_gate.py",
            "--target",
            commit,
            "--summary-only",
            "--fail-on-gate",
        ],
        timeout_seconds=timeout,
    )
    line_value = json.loads(line.pop("_output"))
    gate = line_value["gate"]
    line.update(
        {
            "status": "passed",
            "slice_minimum": gate["slice_minimum"],
            "slice_effective": gate["slice_effective"],
            "parent_minimum": gate["parent_minimum"],
            "parent_effective": gate["parent_effective"],
        }
    )

    focused = run_command(
        [
            python,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            ".tmp/pytest-m3-s02a02-focused",
            "tests/unit/test_m3_live_benchmark.py",
        ],
        timeout_seconds=timeout,
    )
    focused_output = focused.pop("_output")
    focused["status"] = "passed"
    focused["tests"] = test_count(focused_output)

    adjacent = run_command(
        [
            python,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            ".tmp/pytest-m3-s02a02-adjacent",
            "tests/scenarios/test_m2_s05_03_experiment_matrix.py",
            "tests/integration/test_experiment_evidence_api_main_path.py",
            "tests/unit/test_m3_regression_hardening.py",
            "tests/unit/test_m3_live_benchmark.py",
        ],
        timeout_seconds=timeout,
    )
    adjacent_output = adjacent.pop("_output")
    adjacent["status"] = "passed"
    adjacent["tests"] = test_count(adjacent_output)

    runtime = run_command(
        [python, "scripts/verify_m3.py", "--runtime-only"],
        timeout_seconds=timeout,
    )
    runtime.pop("_output")
    runtime["status"] = "passed"

    boundary = source_boundary(commit)
    parent_closeout = {
        "status": "passed",
        "minimum": gate["parent_minimum"],
        "effective": gate["parent_effective"],
        "line_gate_output_sha256": line["output_sha256"],
        "cross_slice_runtime_probe_sha256": runtime["output_sha256"],
    }
    receipt = {
        "schema": "zyra.m3-s02a02-validation-receipt/v1",
        "implementation_commit": commit,
        "line_gate": line,
        "focused_validation": focused,
        "adjacent_regression": adjacent,
        "source_boundary": boundary,
        "parent_closeout": parent_closeout,
        "no_new_provider_call": True,
        "external_model_request_made": False,
    }
    receipt["receipt_digest"] = sha256_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    )
    output = Path(arguments.output).resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
