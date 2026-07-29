from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    PROJECT_ROOT / "packages" / "integrations",
    PROJECT_ROOT / "packages" / "workers",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import (  # noqa: E402
    browser_use_source_identity,
    claude_code_source_identity,
)
from zyra_workers import CodeWorkerSidecarClient, code_worker_entrypoint  # noqa: E402


def _runtime_probe(project_root: Path) -> dict[str, Any]:
    client = CodeWorkerSidecarClient(project_root)
    health = client.health()
    inventory = client.runtime_inventory()
    query = client.query_contract()
    session = client.session_contract()
    tools = client.tool_loop_contract()
    entrypoint = code_worker_entrypoint(project_root)
    entrypoint_text = entrypoint.read_text(encoding="utf-8")
    sources = [
        claude_code_source_identity().to_dict(),
        browser_use_source_identity().to_dict(),
    ]
    legacy_roots = {
        root: (project_root / root).exists()
        for root in ("vendor", "vendor-runtimes")
    }
    checks = {
        "legacy_roots_absent": not any(legacy_roots.values()),
        "source_identities_metadata_only": all(
            source["status"] == "retired"
            and source["availability"] == "not_applicable"
            and source["filesystem_required"] is False
            and source["fallback_available"] is False
            for source in sources
        ),
        "formal_runtime_complete": (
            health.get("ok") is True
            and health.get("canonicalOwner") == "typescript"
            and health.get("requiresRootSourceRepo") is False
            and health.get("requiresVendorRuntime") is False
            and health.get("productizedRuntime", {}).get("complete") is True
        ),
        "formal_modules_reachable": (
            inventory.get("productizedRuntime", {})
            .get("moduleChecks", {})
            .get("queryEngine")
            is True
            and inventory.get("productizedRuntime", {})
            .get("moduleChecks", {})
            .get("toolOrchestration")
            is True
        ),
        "formal_contracts_reachable": (
            query.get("canonicalOwner") == "typescript"
            and query.get("requiresVendorRuntime") is False
            and session.get("canonicalOwner") == "typescript"
            and tools.get("resultBudgetOwner") == "typescript"
        ),
        "formal_entrypoint_reachable": (
            entrypoint.exists()
            and entrypoint.relative_to(project_root).as_posix()
            == "apps/code-worker/src/main.ts"
            and "runStdioRuntime" in entrypoint_text
            and "vendor/claude-code-best" not in entrypoint_text
            and "vendor-runtimes" not in entrypoint_text
        ),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "legacy_roots": legacy_roots,
        "source_identities": sources,
        "entrypoint": entrypoint.relative_to(project_root).as_posix(),
        "health": health,
        "inventory": inventory,
        "query_contract": query,
        "session_contract": session,
        "tool_loop_contract": tools,
    }


def _run_clean_source_probe() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        clean_root = Path(tmpdir) / "zyra-clean"
        _copy_clean_project(clean_root)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            str(clean_root / "packages" / name)
            for name in ("integrations", "workers")
        )
        # The clean source tree intentionally omits dependencies. Reuse only the
        # pinned host toolchain executable; no runtime source is resolved from
        # the checkout that launched this probe.
        host_tool_bin = PROJECT_ROOT / "node_modules" / ".bin"
        env["PATH"] = os.pathsep.join(
            (str(host_tool_bin), env.get("PATH", ""))
        )
        bootstrap = subprocess.run(
            [
                str(host_tool_bin / "bun.exe"),
                "install",
                "--offline",
                "--frozen-lockfile",
            ],
            cwd=clean_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if bootstrap.returncode != 0:
            return {
                "ok": False,
                "phase": "dependency_bootstrap",
                "returncode": bootstrap.returncode,
                "stdout": bootstrap.stdout,
                "stderr": bootstrap.stderr,
                "legacy_roots": {
                    root: (clean_root / root).exists()
                    for root in ("vendor", "vendor-runtimes")
                },
                "nested_checks": {},
            }
        build = subprocess.run(
            [str(host_tool_bin / "bun.exe"), "run", "build:bun"],
            cwd=clean_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if build.returncode != 0:
            return {
                "ok": False,
                "phase": "runtime_build",
                "returncode": build.returncode,
                "stdout": build.stdout,
                "stderr": build.stderr,
                "legacy_roots": {
                    root: (clean_root / root).exists()
                    for root in ("vendor", "vendor-runtimes")
                },
                "nested_checks": {},
            }
        built_entrypoint = clean_root / "dist" / "code-worker" / "main.js"
        payloads: dict[str, Any] = {}
        for name, flag in (
            ("health", "--health"),
            ("inventory", "--inventory"),
            ("query", "--query-contract"),
            ("session", "--session-contract"),
            ("tools", "--tool-loop-contract"),
        ):
            completed = subprocess.run(
                [str(host_tool_bin / "bun.exe"), str(built_entrypoint), flag],
                cwd=clean_root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
            if completed.returncode != 0:
                return {
                    "ok": False,
                    "phase": f"built_{name}",
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "legacy_roots": {
                        root: (clean_root / root).exists()
                        for root in ("vendor", "vendor-runtimes")
                    },
                    "nested_checks": {},
                }
            payloads[name] = json.loads(completed.stdout.splitlines()[0])
        nested_checks = {
            "legacy_roots_absent": not any(
                (clean_root / root).exists()
                for root in ("vendor", "vendor-runtimes")
            ),
            "built_runtime_complete": (
                payloads["health"].get("ok") is True
                and payloads["health"].get("canonicalOwner") == "typescript"
                and payloads["health"].get("requiresVendorRuntime") is False
                and payloads["health"].get("requiresRootSourceRepo") is False
            ),
            "built_contracts_reachable": (
                payloads["inventory"].get("ok") is True
                and payloads["query"].get("ok") is True
                and payloads["session"].get("ok") is True
                and payloads["tools"].get("ok") is True
            ),
        }
        return {
            "ok": all(nested_checks.values()),
            "phase": "complete",
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "legacy_roots": {
                root: (clean_root / root).exists()
                for root in ("vendor", "vendor-runtimes")
            },
            "nested_checks": nested_checks,
            "built_entrypoint": "dist/code-worker/main.js",
        }


def _copy_clean_project(clean_root: Path) -> None:
    clean_root.mkdir(parents=True, exist_ok=True)
    for name in ("packages", "apps", "scripts"):
        source = PROJECT_ROOT / name
        if source.exists():
            shutil.copytree(source, clean_root / name, ignore=_copy_ignore)
    runtime_evidence = (
        PROJECT_ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M1-R01-v14"
        / "execution-01-independent-review"
    )
    if runtime_evidence.is_dir():
        shutil.copytree(
            runtime_evidence,
            clean_root
            / "docs"
            / "reviews"
            / "evidence"
            / "M1-R01-v14"
            / "execution-01-independent-review",
        )
    for relative in (
        Path(
            "docs/reviews/evidence/M1-R01-v3/execution-02/"
            "e01-verified-prerequisite.json"
        ),
        Path("docs/reviews/M1-R01-v14-execution-01-independent-review.md"),
    ):
        source = PROJECT_ROOT / relative
        destination = clean_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for optional_name in (
        "pyproject.toml",
        "README.md",
        "package.json",
        "bun.lock",
    ):
        source_file = PROJECT_ROOT / optional_name
        if source_file.exists():
            shutil.copy2(source_file, clean_root / optional_name)


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "tmp",
        "vendor",
        "vendor-runtimes",
        "source-pool",
        "runtime-sources",
    }
    return {name for name in names if name in ignored or name.endswith(".pyc")}


def build_payload(*, run_clean_source: bool) -> dict[str, Any]:
    runtime = _runtime_probe(PROJECT_ROOT)
    clean_source = _run_clean_source_probe() if run_clean_source else None
    return {
        "schema": "zyra.phase2.claude-productization-retirement-verification/v1",
        "ok": bool(runtime["ok"]) and (
            clean_source is None or bool(clean_source["ok"])
        ),
        "project_root": str(PROJECT_ROOT),
        "runtime": runtime,
        "clean_source": clean_source,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the formal Claude TypeScript runtime after retiring legacy "
            "vendor source pools."
        )
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--clean-source",
        dest="clean_source",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--no-clean-source",
        dest="clean_source",
        action="store_false",
    )
    args = parser.parse_args(argv)
    payload = build_payload(run_clean_source=args.clean_source)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"ok={str(payload['ok']).lower()}")
        print(
            "legacy_roots_absent="
            f"{str(payload['runtime']['checks']['legacy_roots_absent']).lower()}"
        )
        if payload["clean_source"] is not None:
            print(
                "clean_source_ok="
                f"{str(payload['clean_source']['ok']).lower()}"
            )
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
