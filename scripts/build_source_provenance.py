from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATIONS_ROOT = PROJECT_ROOT / "packages" / "integrations"
if str(INTEGRATIONS_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATIONS_ROOT))

from zyra_integrations.mcp.source_audit import MCP_SOURCE_DECISIONS
from zyra_integrations.source_provenance import PROVENANCE_SCHEMA


REPOSITORIES: Mapping[str, Mapping[str, str]] = {
    "claude-code-best": {
        "commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        "license": "USER-AUTHORIZED-PROJECT-REUSE",
        "license_path": "",
    },
    "agent-framework": {
        "commit": "d50698bb797710bfd1ebf34eb621c905a4009b2d",
        "license": "MIT",
        "license_path": "LICENSE",
    },
    "opencode": {
        "commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "license": "MIT",
        "license_path": "LICENSE",
    },
    "agentscope": {
        "commit": "b6698c5dbaa1aa916925e27402767f45e2405fa4",
        "license": "Apache-2.0",
        "license_path": "LICENSE",
    },
    "hermes-agent": {
        "commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "license": "MIT",
        "license_path": "LICENSE",
    },
    "OpenHands": {
        "commit": "c105a82387898e744423c8831d412e26495b38a9",
        "license": "MIT (non-enterprise paths)",
        "license_path": "LICENSE",
    },
    "oh-my-pi": {
        "commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "license": "MIT",
        "license_path": "LICENSE",
    },
}

M2_AUDIT_OWNERS = {
    "M2-S01A-01",
    "M2-S01A-02",
    "M2-S01B-01",
    "M2-S01B-02",
}

SOURCE_GRAPHS = {
    "claude-code-best": "batch-05-mcp-runtime-tools-auth.md",
    "agent-framework": "batch-02-tools-skills-mcp-middleware.md",
    "opencode": "batch-06-mcp-plugin-acp-control-plane.md",
    "agentscope": "batch-04-mcp-workspace-gateway-security.md",
    "hermes-agent": "batch-08-plugins-providers-mcp-acp-source-verdict.md",
}

LEGACY_SOURCE_GRAPH_INPUTS = (
    "source-graphs/claude-code-best/source-graph.md",
    "source-graphs/langgraph/source-graph.md",
    "source-graphs/oh-my-pi/source-to-target.md",
    "docs/milestones/source-graph-realignment-2026-07-08.md",
)

PHASE2_ANALYSES = (
    "LoopX.md",
    "ARG-Designer.md",
    "CARD.md",
    "AgentPrune.md",
    "MaAS.md",
)

AUTHORITY_MANIFESTS = (
    "execution-01-source-manifest.jsonl",
    "execution-02-source-manifest.jsonl",
    "execution-03-source-manifest.jsonl",
    "execution-04-baseline-receipt.json",
    "execution-04-source-recovery-manifest.jsonl",
    "execution-04-target-provenance-map.jsonl",
    "execution-04-reimplementation-exceptions.jsonl",
    "execution-04-python-owner-census.jsonl",
    "execution-04-mutation-manifest.jsonl",
    "execution-04-gate-profile.json",
)

FOUNDATION_PRIMARY_SOURCE_PATHS = (
    "src/Tool.ts",
    "src/tools.ts",
    "src/commands.ts",
    "src/commands/compact",
    "src/commands/context",
    "src/tools/AgentTool/forkSubagent.ts",
    "src/entrypoints/cli.tsx",
    "src/commands/doctor",
)

FOUNDATION_REFERENCE_PATHS = (
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/01-query-engine.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/02-tool-system.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/04-plugin-system.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/07-permission-pipeline.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/08-agent-swarms.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/10-context-assembly.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/11-compact-system.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/13-bridge-system.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/14-ui-state-management.md",
    "claudecode-related/claude-reviews-claude/architecture/zh-CN/15-services-api-layer.md",
    "claudecode-related/Dive-into-Claude-Code/docs/build-your-own-agent_zh.md",
)

LEDGER_IDENTITY_INDEX = (
    "packages/integrations/zyra_integrations/data/internalization_ledger_seed.json"
)

LOOPX_TAG = "v0.2.13"
LOOPX_TAG_OBJECT = "a2c072d412d90839132e1cf39c23dd431c394175"
LOOPX_COMMIT = "7232dca45ec2ca996edc43b2d3558edc802c844e"


def _git(repository: Path, *arguments: str, binary: bool = False) -> str | bytes:
    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=not binary,
        timeout=120,
    )
    if completed.returncode:
        stderr = (
            completed.stderr.decode("utf-8", errors="replace")
            if binary
            else str(completed.stderr)
        )
        raise RuntimeError(f"git {' '.join(arguments)} failed: {stderr.strip()}")
    return completed.stdout


def _git_object_exists(repository: Path, commit: str, source_path: str) -> bool:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-C",
            str(repository),
            "cat-file",
            "-e",
            f"{commit}:{source_path}",
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    return completed.returncode == 0


def _expand_git_paths(
    repository: Path,
    commit: str,
    requested_paths: Sequence[str],
) -> set[str]:
    expanded: set[str] = set()
    for source_path in requested_paths:
        output = str(
            _git(
                repository,
                "ls-tree",
                "-r",
                "--name-only",
                commit,
                "--",
                source_path,
            )
        )
        matches = {line.strip() for line in output.splitlines() if line.strip()}
        if not matches:
            raise RuntimeError(
                f"required provenance source is missing: {repository.name}:{source_path}"
            )
        expanded.update(matches)
    return expanded


def _copy(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"provenance input is missing or unsafe: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _write_git_blob(repository: Path, commit: str, source_path: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(bytes(_git(repository, "show", f"{commit}:{source_path}", binary=True)))


def _expand_braces(value: str) -> list[str]:
    start = value.find("{")
    if start < 0:
        return [value]
    end = value.find("}", start + 1)
    if end < 0:
        return [value]
    prefix = value[:start]
    suffix = value[end + 1 :]
    expanded: list[str] = []
    for choice in value[start + 1 : end].split(","):
        expanded.extend(_expand_braces(f"{prefix}{choice}{suffix}"))
    return expanded


def _m2_audit_sources() -> list[tuple[str, str, str]]:
    ledger_path = (
        PROJECT_ROOT
        / "packages"
        / "integrations"
        / "zyra_integrations"
        / "data"
        / "internalization_ledger_seed.json"
    )
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    sources: set[tuple[str, str, str]] = set()
    for entry in document["entries"]:
        if str(entry.get("owner_unit") or "") not in M2_AUDIT_OWNERS:
            continue
        repository = str(entry.get("source_repo") or "")
        metadata = entry.get("metadata") or {}
        commit = str(metadata.get("source_commit") or "")
        if repository not in REPOSITORIES:
            continue
        if commit != REPOSITORIES[repository]["commit"]:
            raise RuntimeError(
                f"M2 source identity mismatch: {repository}: {commit}"
            )
        for group in str(entry.get("source_path") or "").split(";"):
            normalized = group.strip().replace("\\", "/")
            if not normalized:
                continue
            for source_path in _expand_braces(normalized):
                sources.add((repository, commit, source_path))
    return sorted(sources)


def _e04_recovery_sources(authority_source: Path) -> list[tuple[str, str, str]]:
    manifest = authority_source / "execution-04-source-recovery-manifest.jsonl"
    sources: set[tuple[str, str, str]] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        repository = str(record.get("source_repo") or "")
        commit = str(record.get("source_snapshot") or "")
        source_path = str(record.get("source_path") or "")
        if repository not in REPOSITORIES:
            raise RuntimeError(f"unknown E04 recovery repository: {repository}")
        if commit != REPOSITORIES[repository]["commit"]:
            raise RuntimeError(
                f"E04 source identity mismatch: {repository}: {commit}"
            )
        sources.add((repository, commit, source_path))
    return sorted(sources)


def _record(
    root: Path,
    path: Path,
    *,
    category: str,
    source_repository: str = "",
    source_path: str = "",
) -> dict[str, Any]:
    payload = path.read_bytes()
    value: dict[str, Any] = {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "category": category,
    }
    if source_repository:
        value["source_repository"] = source_repository
    if source_path:
        value["source_path"] = PurePosixPath(source_path).as_posix()
    return value


def _utf8_lf_sha256(path: Path) -> str:
    normalized = (
        path.read_text(encoding="utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .encode("utf-8")
    )
    return hashlib.sha256(normalized).hexdigest()


def build(*, workspace_root: Path, output_root: Path) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise RuntimeError(f"provenance output already exists: {output_root}")
    if output_root.parent != PROJECT_ROOT:
        raise RuntimeError("provenance output must be a direct child of the Zyra project root")
    output_root.mkdir(parents=False, exist_ok=False)

    records: list[dict[str, Any]] = []
    repositories: dict[str, Any] = {}
    for name, identity in REPOSITORIES.items():
        repository = workspace_root / name
        observed = str(_git(repository, "rev-parse", "HEAD")).strip()
        if observed != identity["commit"]:
            raise RuntimeError(
                f"source repository identity mismatch: {name}: {observed} != {identity['commit']}"
            )
        repositories[name] = dict(identity)

    authority_source = (
        workspace_root
        / "docs"
        / "remediations"
        / "M1-R01-claude-source-custody"
        / "manifests"
    )
    requested_sources = {
        (
            decision.repository,
            REPOSITORIES[decision.repository]["commit"],
            decision.source_path,
        )
        for decision in MCP_SOURCE_DECISIONS
    }
    requested_sources.update(
        item
        for item in _m2_audit_sources()
        if _git_object_exists(workspace_root / item[0], item[1], item[2])
    )
    requested_sources.update(_e04_recovery_sources(authority_source))
    requested_sources.update(
        (
            "claude-code-best",
            REPOSITORIES["claude-code-best"]["commit"],
            source_path,
        )
        for source_path in _expand_git_paths(
            workspace_root / "claude-code-best",
            REPOSITORIES["claude-code-best"]["commit"],
            FOUNDATION_PRIMARY_SOURCE_PATHS,
        )
    )
    for repository_name, commit, source_path in sorted(requested_sources):
        repository = workspace_root / repository_name
        destination = output_root / repository_name / source_path
        _write_git_blob(repository, commit, source_path, destination)
        records.append(
            _record(
                output_root,
                destination,
                category="repository_source",
                source_repository=repository_name,
                source_path=source_path,
            )
        )

    for name, identity in REPOSITORIES.items():
        license_path = identity["license_path"]
        if not license_path:
            continue
        destination = output_root / name / license_path
        if not destination.exists():
            _write_git_blob(
                workspace_root / name,
                identity["commit"],
                license_path,
                destination,
            )
            records.append(
                _record(
                    output_root,
                    destination,
                    category="repository_license",
                    source_repository=name,
                    source_path=license_path,
                )
            )

    for repository, filename in SOURCE_GRAPHS.items():
        source = workspace_root / "source-graphs" / repository / filename
        destination = output_root / "source-graphs" / repository / filename
        _copy(source, destination)
        records.append(_record(output_root, destination, category="source_graph"))

    for relative in FOUNDATION_REFERENCE_PATHS:
        source = workspace_root.joinpath(*PurePosixPath(relative).parts)
        destination = output_root.joinpath(*PurePosixPath(relative).parts)
        _copy(source, destination)
        records.append(
            _record(output_root, destination, category="reference_source")
        )

    for relative in LEGACY_SOURCE_GRAPH_INPUTS:
        source = workspace_root.joinpath(*PurePosixPath(relative).parts)
        destination = output_root.joinpath(*PurePosixPath(relative).parts)
        _copy(source, destination)
        records.append(_record(output_root, destination, category="source_graph"))

    for filename in PHASE2_ANALYSES:
        source = workspace_root / "long-horizon-systems" / "project-analysis-notes" / filename
        destination = output_root / "phase2-analysis" / filename
        _copy(source, destination)
        records.append(_record(output_root, destination, category="phase2_analysis"))

    authority_destination = (
        output_root
        / "authority"
        / "m1-r01-claude-source-custody"
        / "manifests"
    )
    for filename in AUTHORITY_MANIFESTS:
        source = authority_source / filename
        destination = authority_destination / filename
        _copy(source, destination)
        records.append(_record(output_root, destination, category="authority_manifest"))

    loopx_repository = workspace_root / "long-horizon-systems" / "loopx"
    observed_tag = str(_git(loopx_repository, "rev-parse", LOOPX_TAG)).strip()
    observed_commit = str(
        _git(loopx_repository, "rev-parse", f"{LOOPX_TAG}^{{commit}}")
    ).strip()
    if observed_tag != LOOPX_TAG_OBJECT or observed_commit != LOOPX_COMMIT:
        raise RuntimeError(
            f"LoopX identity mismatch: tag={observed_tag}, commit={observed_commit}"
        )
    loopx_root = output_root / "loopx"
    loopx_root.mkdir(parents=True, exist_ok=True)
    archive = loopx_root / "loopx-v0.2.13.tar.gz"
    _git(
        loopx_repository,
        "archive",
        "--format=tar.gz",
        f"--output={archive}",
        LOOPX_TAG,
    )
    tag_object = loopx_root / "v0.2.13.tag-object"
    tag_object.write_bytes(bytes(_git(loopx_repository, "cat-file", "tag", LOOPX_TAG, binary=True)))
    commit_object = loopx_root / "7232dca45ec2.commit-object"
    commit_object.write_bytes(
        bytes(_git(loopx_repository, "cat-file", "commit", LOOPX_COMMIT, binary=True))
    )
    for path, category in (
        (archive, "loopx_source_archive"),
        (tag_object, "loopx_git_object"),
        (commit_object, "loopx_git_object"),
    ):
        records.append(_record(output_root, path, category=category))

    readme = output_root / "README.md"
    readme.write_text(
        "# Zyra bundled source provenance\n\n"
        "This directory is immutable, non-runtime evidence. It contains only the exact "
        "source files reviewed by Zyra, their source graphs, frozen identity indexes, "
        "frozen authority manifests, "
        "Phase 2 mechanism analyses, and the pinned LoopX source archive. Runtime code must "
        "never import or execute files from this directory. Rebuild it only through "
        "`scripts/build_source_provenance.py --workspace-root <explicit-source-workspace>`.\n",
        encoding="utf-8",
        newline="\n",
    )
    records.append(_record(output_root, readme, category="provenance_documentation"))

    manifest: dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA,
        "repositories": repositories,
        "identity_indexes": [
            {
                "kind": "internalization_ledger_source_evidence",
                "path": LEDGER_IDENTITY_INDEX,
                "normalization": "utf8_lf",
                "sha256": _utf8_lf_sha256(
                    PROJECT_ROOT.joinpath(
                        *PurePosixPath(LEDGER_IDENTITY_INDEX).parts
                    )
                ),
            }
        ],
        "loopx": {
            "version": "0.2.13",
            "tag": LOOPX_TAG,
            "tag_object": LOOPX_TAG_OBJECT,
            "commit": LOOPX_COMMIT,
            "archive": "loopx/loopx-v0.2.13.tar.gz",
            "tag_object_path": "loopx/v0.2.13.tag-object",
            "commit_object_path": "loopx/7232dca45ec2.commit-object",
        },
        "files": sorted(records, key=lambda item: str(item["path"]).encode("utf-8")),
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "schema": "zyra.source-provenance-build/v1",
        "ready": True,
        "output": str(output_root),
        "file_count": len(records),
        "repository_count": len(repositories),
        "source_file_count": sum(
            item["category"] == "repository_source" for item in records
        ),
    }


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build Zyra's repository-local immutable source provenance assets."
    )
    parser.add_argument(
        "--workspace-root",
        required=True,
        help="Explicit source workspace used only while refreshing provenance assets.",
    )
    parser.add_argument("--output-root", default=str(PROJECT_ROOT / "provenance"))
    arguments = parser.parse_args(argv)
    result = build(
        workspace_root=Path(arguments.workspace_root),
        output_root=Path(arguments.output_root),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
