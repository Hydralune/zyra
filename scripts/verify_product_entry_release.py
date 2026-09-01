from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "zyra.product-entry-release-verification/v1"
CLI_ENTRY = Path("apps/cli/src/index.ts")
EXPECTED_COMMAND_LINES = (
    "zyra                              product TUI",
    'zyra "<goal>"                     product TUI with an initial goal',
    "zyra resume <task|session>        resume in the product TUI",
    "zyra dev [<goal>]                 developer event interface",
    "zyra events <task|session>        observe raw canonical events",
    "zyra run <goal | -f file | stdin> non-interactive JSONL execution",
    "zyra ls                           list canonical tasks and sessions",
    "zyra scenario <action> [...]      scenario lifecycle over the daemon API",
    "zyra ui [--task <id>]             ensure daemon, start Web, open product route",
    "zyra doctor [--bundle <file>]     read-only product diagnostics",
    "zyra daemon <start|stop|status>   local daemon supervision",
)
FORBIDDEN_DEPENDENCIES = frozenset(
    {
        "react",
        "react-dom",
        "ink",
        "@inkjs/ui",
        "blessed",
        "neo-blessed",
    }
)
FORBIDDEN_ARTIFACT_MARKERS = (
    "react/jsx-runtime",
    "react-dom",
    "@inkjs/ui",
    "node_modules/ink/",
    "apps/web/src/",
    "claude-code-best",
    "document.createElement",
    "createRoot(document",
)


class VerificationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_index(project_root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    index: dict[str, tuple[Path, dict[str, Any]]] = {}
    for base in (project_root / "apps", project_root / "packages"):
        for path in sorted(base.rglob("package.json")):
            relative_parts = path.relative_to(project_root).parts
            if any(part in {"node_modules", "dist", ".tmp"} for part in relative_parts):
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            name = value.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name in index:
                raise VerificationError(f"duplicate workspace package: {name}")
            index[name] = (path, value)
    return index


def audit_cli_dependency_closure(project_root: Path) -> dict[str, Any]:
    index = _manifest_index(project_root)
    root_name = "@zyra/cli"
    if root_name not in index:
        raise VerificationError("CLI package manifest is unavailable")
    pending = [root_name]
    visited: set[str] = set()
    external: set[str] = set()
    forbidden: set[str] = set()
    external_paths: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        _path, manifest = index[name]
        for section in ("dependencies", "optionalDependencies", "peerDependencies"):
            values = manifest.get(section, {})
            if not isinstance(values, Mapping):
                raise VerificationError(f"invalid dependency section: {name}:{section}")
            for dependency, specifier in values.items():
                dependency_name = str(dependency)
                dependency_specifier = str(specifier)
                if dependency_name.casefold() in FORBIDDEN_DEPENDENCIES:
                    forbidden.add(dependency_name)
                folded = dependency_specifier.casefold()
                if (
                    folded.startswith(("file:", "link:"))
                    or "claude-code-best" in folded
                    or dependency_specifier.startswith(("../", "..\\"))
                ):
                    external_paths.add(f"{name}:{dependency_name}")
                if dependency_specifier.startswith("workspace:"):
                    if dependency_name not in index:
                        raise VerificationError(
                            f"missing CLI workspace dependency: {dependency_name}"
                        )
                    pending.append(dependency_name)
                else:
                    external.add(dependency_name)
    ready = not forbidden and not external_paths
    return {
        "ready": ready,
        "root": root_name,
        "workspace_packages": sorted(visited),
        "external_packages": sorted(external),
        "forbidden_dependencies": sorted(forbidden),
        "external_path_dependencies": sorted(external_paths),
        "web_runtime_in_closure": any(
            name in visited for name in ("@zyra/web", "react", "react-dom")
        ),
    }


def audit_node_artifact(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise VerificationError("Node CLI artifact is missing or empty")
    text = path.read_text(encoding="utf-8", errors="replace").replace("\\", "/")
    findings = sorted(
        marker for marker in FORBIDDEN_ARTIFACT_MARKERS if marker.casefold() in text.casefold()
    )
    return {
        "ready": not findings,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "forbidden_runtime_markers": findings,
        "node_shebang": text.startswith("#!/usr/bin/env node"),
        "dom_react_tui_runtime_present": bool(findings),
    }


def _resolve_bun(project_root: Path) -> Path:
    configured = os.environ.get("ZYRA_BUN_EXECUTABLE", "").strip()
    candidates = [
        Path(configured) if configured else None,
        project_root / "node_modules" / ".bin" / (
            "bun.exe" if os.name == "nt" else "bun"
        ),
        Path(shutil.which("bun") or "") if shutil.which("bun") else None,
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise VerificationError("Bun executable is unavailable")


def _resolve_node() -> Path:
    candidate = shutil.which("node")
    if not candidate:
        raise VerificationError("Node executable is unavailable")
    return Path(candidate).resolve()


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "SOURCE_DATE_EPOCH": "1700000000"},
    )
    if completed.returncode != 0:
        raise VerificationError("product-entry release command failed")
    return completed


def _build_once(project_root: Path, bun: Path, output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = _run(
        (
            str(bun),
            "build",
            CLI_ENTRY.as_posix(),
            "--outfile",
            str(output),
            "--target",
            "node",
        ),
        cwd=project_root,
    )
    return {
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
        "artifact_sha256": sha256_file(output),
        "artifact_size": output.stat().st_size,
    }


def _parse_jsonl(value: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in value.splitlines():
        if not line:
            continue
        decoded = json.loads(line)
        if not isinstance(decoded, dict):
            raise VerificationError("CLI JSONL record is not an object")
        records.append(decoded)
    if not records or records[-1].get("type") != "result":
        raise VerificationError("CLI JSONL result record is missing")
    return records


def _probe_entry(
    label: str,
    command: Sequence[str],
    *,
    project_root: Path,
) -> dict[str, Any]:
    completed = _run((*command, "--help"), cwd=project_root, timeout=60.0)
    records = _parse_jsonl(completed.stdout)
    missing = [line for line in EXPECTED_COMMAND_LINES if line not in completed.stderr]
    schemas = sorted({str(record.get("schema") or "") for record in records})
    help_payload = records[0].get("payload")
    command_count = (
        help_payload.get("command_count")
        if isinstance(help_payload, Mapping)
        else None
    )
    return {
        "ready": not missing
        and schemas == ["zyra.cli-record.v1", "zyra.cli-result.v1"]
        and command_count == len(EXPECTED_COMMAND_LINES)
        and records[-1].get("exit_code") == 0,
        "entry": label,
        "returncode": completed.returncode,
        "record_count": len(records),
        "schemas": schemas,
        "exit_code": records[-1].get("exit_code"),
        "command_count": command_count,
        "missing_command_lines": missing,
        "stdout_jsonl": True,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
    }


def _source_revision(project_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    revision = completed.stdout.strip()
    if completed.returncode == 0 and len(revision) == 40:
        return revision
    manifest = project_root / "release" / "manifest.json"
    if manifest.is_file():
        value = json.loads(manifest.read_text(encoding="utf-8"))
        revision = str(value.get("source_commit") or "")
        if len(revision) == 40:
            return revision
    return "unavailable"


def verify_product_entry_release(
    project_root: Path,
    *,
    output: Path,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    output = output if output.is_absolute() else project_root / output
    bun = _resolve_bun(project_root)
    node = _resolve_node()
    temporary_root = project_root / ".tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="product-entry-release-",
        dir=temporary_root,
    ) as directory:
        temporary = Path(directory)
        first_path = temporary / "a" / "zyra.js"
        second_path = temporary / "b" / "zyra.js"
        first = _build_once(project_root, bun, first_path)
        second = _build_once(project_root, bun, second_path)
        reproducible = first["artifact_sha256"] == second["artifact_sha256"]
        if not reproducible:
            raise VerificationError("Node CLI builds are not byte-identical")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(first_path, output)
    artifact = audit_node_artifact(output)
    dependency = audit_cli_dependency_closure(project_root)
    probes = [
        _probe_entry(
            "bun-direct",
            (str(bun), CLI_ENTRY.as_posix()),
            project_root=project_root,
        ),
        _probe_entry(
            "node-built",
            (str(node), str(output)),
            project_root=project_root,
        ),
    ]
    current = platform.system().casefold()
    platforms = {
        name: {
            "status": "passed" if current == name else "unavailable",
            "reason": (
                "executed on current host"
                if current == name
                else f"{name} host was not available in this verification environment"
            ),
        }
        for name in ("windows", "linux", "darwin")
    }
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "ready": artifact["ready"]
        and artifact["node_shebang"]
        and dependency["ready"]
        and not dependency["web_runtime_in_closure"]
        and all(probe["ready"] for probe in probes),
        "source_commit": _source_revision(project_root),
        "build": {
            "command": "bun build apps/cli/src/index.ts --outfile <output> --target node",
            "target": "node",
            "reproducible": True,
            "first": first,
            "second": second,
        },
        "artifact": artifact,
        "dependency_closure": dependency,
        "entry_probes": probes,
        "command_surface": list(EXPECTED_COMMAND_LINES),
        "exit_codes": [0, 1, 2, 3, 4, 5],
        "platforms": platforms,
        "distribution_scope": {
            "npm_publish": False,
            "installer": False,
            "code_signing": False,
        },
    }
    report["digest"] = stable_digest(report)
    if not report["ready"]:
        raise VerificationError("product-entry release verification is not ready")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="dist/cli/zyra.js")
    parser.add_argument("--report", default="dist/product-entry-release.json")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = verify_product_entry_release(
            PROJECT_ROOT,
            output=Path(arguments.output),
        )
    except (OSError, ValueError, VerificationError, subprocess.SubprocessError) as error:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "ready": False,
                    "error": type(error).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    report_path = Path(arguments.report)
    if not report_path.is_absolute():
        report_path = PROJECT_ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
