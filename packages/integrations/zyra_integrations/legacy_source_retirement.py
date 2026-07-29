from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "zyra.phase2.legacy-source-pool-retirement/v1"
TOOL_NAME = "zyra-legacy-source-retirement"
TOOL_VERSION = "1.0.0"
LEGACY_ROOTS = ("vendor", "vendor-runtimes")
FORBIDDEN_POOL_SEGMENTS = {
    "vendor",
    "vendor-runtimes",
    "third_party",
    "runtime-sources",
    "source-pool",
}
LEGACY_REFERENCE_PATTERN = re.compile(
    r"(?:vendor-runtimes[/\\]|vendor[/\\](?:claude-code-best|browser-use)(?:[/\\]|$))",
    re.IGNORECASE,
)
MIGRATION_REQUIRED_PATHS = {
    "apps/api/zyra_api/main.py",
    "packages/integrations/zyra_integrations/vendor_manifest.py",
    "packages/integrations/zyra_integrations/source_extraction.py",
    "packages/integrations/zyra_integrations/reference_crosswalk.py",
    "packages/workers/zyra_workers/browser_worker.py",
    "packages/workers/zyra_workers/runtime_scaffold.py",
    "packages/workers/zyra_workers/scaffold_bridge_runtime.py",
    "scripts/verify_code_worker_sidecar.py",
    "scripts/verify_m2.py",
    "scripts/zyra_source_extract.py",
    "tests/integration/test_browser_worker.py",
    "tests/integration/test_vendor_manifest.py",
    "tests/integration/test_m1_01b_extraction_runtime_scaffold_cli.py",
    "tests/unit/test_m1_01b_runtime_scaffold_acceptance.py",
    "tests/unit/test_source_extraction_runtime_scaffold.py",
}
IMMUTABLE_EVIDENCE_PATHS = (
    "docs/release/phase2-baseline-manifest.json",
    "docs/reviews/P2-S01-04-loopx-v0213-embedded-source-cutover-review.md",
    "docs/reviews/evidence/P2-S01-04/source-identity.json",
    "docs/reviews/evidence/P2-S01-04/verification-summary.json",
    "docs/reviews/evidence/M3-S03-02/critical-review.md",
    "docs/reviews/evidence/M3-S03-02/final-freeze/final-freeze-verification.json",
    "packages/integrations/zyra_integrations/data/internalization_ledger_seed.json",
    "packages/integrations/zyra_integrations/data/source_custody_catalog.json",
)


class RetirementManifestError(RuntimeError):
    """Raised when immutable retirement evidence cannot be produced or verified."""


@dataclass(frozen=True, slots=True)
class GitFile:
    path: str
    mode: str
    blob_id: str
    size: int


@dataclass(frozen=True, slots=True)
class RetirementFinding:
    code: str
    message: str
    path: str = ""
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class RetirementVerification:
    base_commit: str
    target_revision: str
    findings: tuple[RetirementFinding, ...] = field(default_factory=tuple)
    root_file_count: int = 0
    root_total_bytes: int = 0
    historical_target_count: int = 0
    current_legacy_target_count: int = 0

    @property
    def valid(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2.legacy-source-pool-retirement-verification/v1",
            "valid": self.valid,
            "base_commit": self.base_commit,
            "target_revision": self.target_revision,
            "root_file_count": self.root_file_count,
            "root_total_bytes": self.root_total_bytes,
            "historical_target_count": self.historical_target_count,
            "current_legacy_target_count": self.current_legacy_target_count,
            "findings": [item.to_dict() for item in self.findings],
        }


def _run_git(
    project_root: Path,
    arguments: Sequence[str],
    *,
    text: bool = False,
) -> bytes | str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", errors="replace")
        raise RetirementManifestError(
            f"git {' '.join(arguments)} failed with exit {completed.returncode}: {stderr.strip()}"
        )
    return completed.stdout


def resolve_revision(project_root: Path, revision: str) -> str:
    output = _run_git(project_root, ["rev-parse", "--verify", f"{revision}^{{commit}}"], text=True)
    return str(output).strip()


def revision_tree(project_root: Path, revision: str) -> str:
    output = _run_git(project_root, ["rev-parse", f"{revision}^{{tree}}"], text=True)
    return str(output).strip()


def list_git_files(
    project_root: Path,
    revision: str,
    roots: Sequence[str] = (),
) -> tuple[GitFile, ...]:
    arguments = ["ls-tree", "-r", "-l", "-z", revision]
    if roots:
        arguments.extend(["--", *roots])
    raw = _run_git(project_root, arguments)
    assert isinstance(raw, bytes)
    files: list[GitFile] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        mode, object_type, blob_id, size = metadata.decode("ascii").split()
        if object_type != "blob":
            continue
        files.append(
            GitFile(
                path=path_bytes.decode("utf-8", errors="surrogateescape").replace("\\", "/"),
                mode=mode,
                blob_id=blob_id,
                size=int(size),
            )
        )
    return tuple(sorted(files, key=lambda item: item.path))


def read_git_blobs(
    project_root: Path,
    blob_ids: Iterable[str],
) -> dict[str, bytes]:
    ordered = tuple(dict.fromkeys(blob_ids))
    if not ordered:
        return {}
    completed = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=project_root,
        input=b"".join(blob_id.encode("ascii") + b"\n" for blob_id in ordered),
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RetirementManifestError(
            f"git cat-file --batch failed with exit {completed.returncode}: "
            f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
        )
    output = memoryview(completed.stdout)
    offset = 0
    values: dict[str, bytes] = {}
    for requested in ordered:
        newline = completed.stdout.find(b"\n", offset)
        if newline < 0:
            raise RetirementManifestError(
                f"git cat-file response ended before header for {requested}"
            )
        header = bytes(output[offset:newline]).decode("ascii", errors="replace").strip()
        offset = newline + 1
        parts = header.split()
        if len(parts) != 3 or parts[1] != "blob":
            raise RetirementManifestError(
                f"git cat-file returned invalid header for {requested}: {header}"
            )
        actual, _, raw_size = parts
        size = int(raw_size)
        content = bytes(output[offset : offset + size])
        offset += size
        if bytes(output[offset : offset + 1]) != b"\n":
            raise RetirementManifestError(f"git cat-file framing failed for {requested}")
        offset += 1
        values[actual] = content
    return values


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_digest(value: Mapping[str, Any], *, digest_key: str = "manifest_digest") -> str:
    unsigned = dict(value)
    unsigned.pop(digest_key, None)
    return hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


def _tree_digest(files: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["mode"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["blob_id"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _root_tree_id(project_root: Path, revision: str, root: str) -> str:
    output = _run_git(project_root, ["rev-parse", f"{revision}:{root}"], text=True)
    return str(output).strip()


def _reference_category(path: str) -> tuple[str, str]:
    normalized = path.replace("\\", "/")
    if normalized in MIGRATION_REQUIRED_PATHS:
        return "migration_required", "Remove physical source-pool dependency during P2-S02A-01."
    if normalized.startswith("docs/reviews/evidence/") or normalized.startswith(
        ("docs/reviews/", "docs/release/")
    ):
        return "immutable_historical_evidence", "Retain as immutable evidence; never execute as runtime input."
    if normalized.endswith(
        (
            "data/internalization_ledger_seed.json",
            "data/source_custody_catalog.json",
        )
    ):
        return "protected_historical_ledger", "Retain frozen first-stage lineage facts; current target overlay must be zero."
    if normalized.startswith("tests/"):
        return "negative_guard_test", "Reference is permitted only as a denial, fixture, or historical-policy assertion."
    if normalized.startswith("scripts/") or any(
        marker in normalized
        for marker in (
            "source_custody",
            "freeze_audit",
            "productization",
            "release/",
            "source_boundary",
            "source_graph",
            "runtime_events/custody",
            "sandbox_gateway",
        )
    ):
        return "deny_policy", "Reference is a fail-closed exclusion or historical audit label."
    if normalized.endswith((".md", ".json", ".yaml", ".yml")):
        return "documentation_or_metadata", "Reference is non-executable documentation or metadata."
    return "runtime_review_required", "Unclassified production reference must be removed or explicitly denied."


def _reference_inventory(
    project_root: Path,
    revision: str,
    files: Sequence[GitFile],
) -> tuple[dict[str, Any], ...]:
    candidates = [item for item in files if not _under_legacy_root(item.path)]
    blobs = read_git_blobs(project_root, (item.blob_id for item in candidates))
    inventory: list[dict[str, Any]] = []
    for item in candidates:
        raw = blobs[item.blob_id]
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", errors="replace")
        matches = tuple(LEGACY_REFERENCE_PATTERN.finditer(text))
        if not matches:
            continue
        line_numbers = sorted({text.count("\n", 0, match.start()) + 1 for match in matches})
        category, disposition = _reference_category(item.path)
        inventory.append(
            {
                "path": item.path,
                "blob_id": item.blob_id,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "reference_count": len(matches),
                "line_numbers": line_numbers,
                "category": category,
                "disposition": disposition,
            }
        )
    return tuple(sorted(inventory, key=lambda item: str(item["path"])))


def _historical_legacy_target_count(raw: bytes) -> int:
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return 0
    count = 0
    for entry in document.get("entries", []):
        for binding in entry.get("target_bindings", []):
            path = str(binding.get("target_path") or binding.get("path") or "")
            if _under_legacy_root(path):
                count += 1
    return count


def _source_identities(blob_sha256: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "name": "claude-code-best",
            "historical_root": "vendor/claude-code-best",
            "source_commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
            "version": "1.0.0",
            "source_role": "primary_implementation",
            "historical_owner": "TypeScript CodeWorkerRuntime",
            "current_runtime_boundary": "packages/runtime/claude-runtime",
            "license_id": "USER-AUTHORIZED-PROJECT-REUSE",
            "snapshot_license_file": None,
            "identity_file": "vendor/claude-code-best/ZYRA_VENDOR.md",
            "identity_sha256": blob_sha256["vendor/claude-code-best/ZYRA_VENDOR.md"],
            "retirement_status": "retired_not_runtime_available",
        },
        {
            "name": "browser-use",
            "historical_root": "vendor/browser-use",
            "source_commit": "18484f23ac96bb955259a1c54530a7d265dfffdb",
            "version": "0.13.3",
            "source_role": "primary_implementation",
            "historical_owner": "BrowserWorkerRuntime",
            "current_runtime_boundary": "packages/workers/zyra_workers/browser_session",
            "license_id": "MIT",
            "snapshot_license_file": "vendor/browser-use/LICENSE",
            "license_sha256": blob_sha256["vendor/browser-use/LICENSE"],
            "identity_file": "vendor/browser-use/ZYRA_VENDOR.md",
            "identity_sha256": blob_sha256["vendor/browser-use/ZYRA_VENDOR.md"],
            "retirement_status": "retired_not_runtime_available",
        },
        {
            "name": "claude-code-runtime",
            "historical_root": "vendor-runtimes/claude-code-runtime",
            "source_commit": None,
            "version": "0.1.0-m1-02a",
            "source_role": "runtime_assets_vendor_like",
            "historical_owner": "M1 source extraction scaffold",
            "current_runtime_boundary": "packages/runtime/claude-runtime",
            "license_id": "inherits_source_custody",
            "identity_file": "vendor-runtimes/claude-code-runtime/package.json",
            "identity_sha256": blob_sha256[
                "vendor-runtimes/claude-code-runtime/package.json"
            ],
            "retirement_status": "retired_not_runtime_available",
        },
    ]


def freeze_retirement_manifest(
    project_root: str | Path,
    *,
    base_revision: str = "HEAD",
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    base_commit = resolve_revision(root, base_revision)
    base_tree = revision_tree(root, base_commit)
    legacy_files = list_git_files(root, base_commit, LEGACY_ROOTS)
    if not legacy_files:
        raise RetirementManifestError(
            f"{base_commit} does not contain both required legacy source roots"
        )
    blobs = read_git_blobs(root, (item.blob_id for item in legacy_files))
    blob_sha256 = {
        item.path: hashlib.sha256(blobs[item.blob_id]).hexdigest()
        for item in legacy_files
    }
    root_records: list[dict[str, Any]] = []
    for legacy_root in LEGACY_ROOTS:
        files = [
            {
                "path": item.path,
                "relative_path": item.path.removeprefix(f"{legacy_root}/"),
                "mode": item.mode,
                "size": item.size,
                "blob_id": item.blob_id,
                "sha256": blob_sha256[item.path],
            }
            for item in legacy_files
            if item.path == legacy_root or item.path.startswith(f"{legacy_root}/")
        ]
        root_records.append(
            {
                "path": legacy_root,
                "git_tree_id": _root_tree_id(root, base_commit, legacy_root),
                "file_count": len(files),
                "total_bytes": sum(int(item["size"]) for item in files),
                "tree_digest": _tree_digest(files),
                "files": files,
            }
        )

    all_files = list_git_files(root, base_commit)
    all_by_path = {item.path: item for item in all_files}
    missing_evidence = [path for path in IMMUTABLE_EVIDENCE_PATHS if path not in all_by_path]
    if missing_evidence:
        raise RetirementManifestError(
            f"immutable evidence paths missing at {base_commit}: {missing_evidence}"
        )
    evidence_blobs = read_git_blobs(
        root,
        (all_by_path[path].blob_id for path in IMMUTABLE_EVIDENCE_PATHS),
    )
    immutable_evidence = [
        {
            "path": path,
            "blob_id": all_by_path[path].blob_id,
            "sha256": hashlib.sha256(
                evidence_blobs[all_by_path[path].blob_id]
            ).hexdigest(),
        }
        for path in IMMUTABLE_EVIDENCE_PATHS
    ]
    references = _reference_inventory(root, base_commit, all_files)
    ledger_path = (
        "packages/integrations/zyra_integrations/data/"
        "internalization_ledger_seed.json"
    )
    ledger_raw = evidence_blobs[all_by_path[ledger_path].blob_id]
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "slice_id": "P2-S02A-01",
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "frozen_source": {
            "commit": base_commit,
            "tree": base_tree,
            "read_mode": "git_objects_only",
        },
        "retired_roots": root_records,
        "source_identities": _source_identities(blob_sha256),
        "reference_inventory": {
            "file_count": len(references),
            "reference_count": sum(
                int(item["reference_count"]) for item in references
            ),
            "files": list(references),
        },
        "immutable_evidence": immutable_evidence,
        "custody_transition": {
            "historical_ledger_path": ledger_path,
            "historical_ledger_blob_id": all_by_path[ledger_path].blob_id,
            "historical_legacy_target_count": _historical_legacy_target_count(
                ledger_raw
            ),
            "current_legacy_target_count_required": 0,
            "historical_target_interpretation": (
                "Frozen first-stage targets at frozen_source.commit; never current runtime paths."
            ),
        },
        "target_policy": {
            "required_absent_roots": list(LEGACY_ROOTS),
            "forbidden_pool_segments": sorted(FORBIDDEN_POOL_SEGMENTS),
            "renamed_blob_pool_allowed": False,
            "runtime_filesystem_fallback_allowed": False,
            "historical_evidence_rewrite_allowed": False,
            "loopx_root_protected": "packages/integrations/loopx_runtime",
        },
        "declaration": (
            "The retired roots are provenance-only at the frozen commit. No runtime, "
            "test, audit, release, cleanroom, install, doctor, or recovery path may "
            "load them or recreate them under another source-pool name."
        ),
    }
    manifest["manifest_digest"] = payload_digest(manifest)
    return manifest


def _under_legacy_root(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized in LEGACY_ROOTS or normalized.startswith(
        tuple(f"{root}/" for root in LEGACY_ROOTS)
    )


def _forbidden_pool_path(path: str) -> bool:
    parts = {part.lower() for part in PurePosixPath(path.replace("\\", "/")).parts}
    return bool(parts & FORBIDDEN_POOL_SEGMENTS)


def verify_retirement_manifest(
    project_root: str | Path,
    manifest: Mapping[str, Any],
    *,
    target_revision: str = "HEAD",
    check_worktree: bool = True,
) -> RetirementVerification:
    root = Path(project_root).resolve()
    findings: list[RetirementFinding] = []
    expected_digest = str(manifest.get("manifest_digest") or "")
    actual_digest = payload_digest(manifest)
    if expected_digest != actual_digest:
        findings.append(
            RetirementFinding(
                "manifest_digest_mismatch",
                f"manifest digest {expected_digest!r} does not match {actual_digest!r}",
            )
        )
    if manifest.get("schema") != SCHEMA:
        findings.append(
            RetirementFinding(
                "manifest_schema_invalid",
                f"expected {SCHEMA}, received {manifest.get('schema')!r}",
            )
        )
    frozen = manifest.get("frozen_source") or {}
    base_commit = str(frozen.get("commit") or "")
    try:
        resolved_base = resolve_revision(root, base_commit)
    except RetirementManifestError as error:
        findings.append(RetirementFinding("base_commit_unavailable", str(error)))
        return RetirementVerification(
            base_commit=base_commit,
            target_revision=target_revision,
            findings=tuple(findings),
        )
    if resolved_base != base_commit:
        findings.append(
            RetirementFinding(
                "base_commit_not_canonical",
                f"manifest commit {base_commit} resolves to {resolved_base}",
            )
        )
    if revision_tree(root, base_commit) != str(frozen.get("tree") or ""):
        findings.append(
            RetirementFinding(
                "base_tree_mismatch",
                "frozen source tree no longer matches the manifest",
            )
        )

    actual_legacy_files = list_git_files(root, base_commit, LEGACY_ROOTS)
    actual_blobs = read_git_blobs(root, (item.blob_id for item in actual_legacy_files))
    by_path = {item.path: item for item in actual_legacy_files}
    root_count = 0
    root_bytes = 0
    for root_record in manifest.get("retired_roots", []):
        legacy_root = str(root_record.get("path") or "")
        declared_files = root_record.get("files") or []
        recomputed_files: list[dict[str, Any]] = []
        for declared in declared_files:
            path = str(declared.get("path") or "")
            actual = by_path.get(path)
            if actual is None:
                findings.append(
                    RetirementFinding(
                        "frozen_file_missing",
                        "frozen file is not present in the base Git object",
                        path,
                    )
                )
                continue
            raw = actual_blobs[actual.blob_id]
            sha256 = hashlib.sha256(raw).hexdigest()
            if (
                actual.mode != str(declared.get("mode"))
                or actual.blob_id != str(declared.get("blob_id"))
                or actual.size != int(declared.get("size", -1))
                or sha256 != str(declared.get("sha256"))
            ):
                findings.append(
                    RetirementFinding(
                        "frozen_file_mismatch",
                        "mode, size, blob id, or SHA-256 differs from base Git object",
                        path,
                    )
                )
            recomputed_files.append(
                {
                    "path": actual.path,
                    "relative_path": actual.path.removeprefix(f"{legacy_root}/"),
                    "mode": actual.mode,
                    "size": actual.size,
                    "blob_id": actual.blob_id,
                    "sha256": sha256,
                }
            )
        actual_paths = {
            item.path
            for item in actual_legacy_files
            if item.path.startswith(f"{legacy_root}/")
        }
        declared_paths = {str(item.get("path") or "") for item in declared_files}
        if actual_paths != declared_paths:
            findings.append(
                RetirementFinding(
                    "frozen_inventory_mismatch",
                    "declared file inventory differs from base Git tree",
                    legacy_root,
                )
            )
        root_count += len(actual_paths)
        root_bytes += sum(by_path[path].size for path in actual_paths)
        if (
            len(actual_paths) != int(root_record.get("file_count", -1))
            or sum(by_path[path].size for path in actual_paths)
            != int(root_record.get("total_bytes", -1))
            or _root_tree_id(root, base_commit, legacy_root)
            != str(root_record.get("git_tree_id"))
            or _tree_digest(recomputed_files)
            != str(root_record.get("tree_digest"))
        ):
            findings.append(
                RetirementFinding(
                    "frozen_root_mismatch",
                    "root count, bytes, tree id, or independent digest differs",
                    legacy_root,
                )
            )

    base_files = list_git_files(root, base_commit)
    base_by_path = {item.path: item for item in base_files}
    evidence_paths = [
        str(item.get("path") or "")
        for item in manifest.get("immutable_evidence", [])
    ]
    evidence_blobs = read_git_blobs(
        root,
        (
            base_by_path[path].blob_id
            for path in evidence_paths
            if path in base_by_path
        ),
    )
    for evidence in manifest.get("immutable_evidence", []):
        path = str(evidence.get("path") or "")
        actual = base_by_path.get(path)
        if actual is None:
            findings.append(
                RetirementFinding(
                    "immutable_evidence_missing",
                    "immutable evidence is absent from frozen commit",
                    path,
                )
            )
            continue
        sha256 = hashlib.sha256(evidence_blobs[actual.blob_id]).hexdigest()
        if (
            actual.blob_id != str(evidence.get("blob_id"))
            or sha256 != str(evidence.get("sha256"))
        ):
            findings.append(
                RetirementFinding(
                    "immutable_evidence_mismatch",
                    "immutable evidence blob or SHA-256 differs",
                    path,
                )
            )

    try:
        target_commit = resolve_revision(root, target_revision)
        target_files = list_git_files(root, target_commit)
    except RetirementManifestError as error:
        findings.append(RetirementFinding("target_revision_unavailable", str(error)))
        target_commit = target_revision
        target_files = ()
    target_by_path = {item.path: item for item in target_files}
    for path in target_by_path:
        if _forbidden_pool_path(path):
            findings.append(
                RetirementFinding(
                    "forbidden_source_pool_path",
                    "target revision contains a forbidden source-pool path",
                    path,
                )
            )
    legacy_blob_ids = {item.blob_id for item in actual_legacy_files}
    base_nonlegacy_blobs = {
        (item.path, item.blob_id)
        for item in base_files
        if not _under_legacy_root(item.path)
    }
    for item in target_files:
        if (
            item.blob_id in legacy_blob_ids
            and (item.path, item.blob_id) not in base_nonlegacy_blobs
        ):
            findings.append(
                RetirementFinding(
                    "renamed_source_pool_blob",
                    "legacy blob was copied to a new target path after the freeze",
                    item.path,
                )
            )

    target_references = _reference_inventory(root, target_commit, target_files)
    for reference in target_references:
        if reference["category"] in {
            "migration_required",
            "runtime_review_required",
        }:
            findings.append(
                RetirementFinding(
                    "active_legacy_reference",
                    f"active reference category is {reference['category']}",
                    str(reference["path"]),
                )
            )

    if check_worktree:
        for legacy_root in LEGACY_ROOTS:
            candidate = (root / legacy_root).resolve()
            if candidate.exists():
                findings.append(
                    RetirementFinding(
                        "legacy_root_present_in_worktree",
                        "retired source root still exists in the worktree",
                        legacy_root,
                    )
                )

    policy_path = root / "packages" / "productization" / "zyra_productization" / "release" / "policy.py"
    if policy_path.exists():
        policy_text = policy_path.read_text(encoding="utf-8")
        for legacy_root in LEGACY_ROOTS:
            if f'"{legacy_root}"' not in policy_text:
                findings.append(
                    RetirementFinding(
                        "release_deny_root_missing",
                        "release policy no longer denies the retired root",
                        legacy_root,
                    )
                )

    historical_count = int(
        (manifest.get("custody_transition") or {}).get(
            "historical_legacy_target_count", 0
        )
    )
    current_count = sum(
        int(item["reference_count"])
        for item in target_references
        if item["category"] in {"migration_required", "runtime_review_required"}
    )
    return RetirementVerification(
        base_commit=base_commit,
        target_revision=target_commit,
        findings=tuple(findings),
        root_file_count=root_count,
        root_total_bytes=root_bytes,
        historical_target_count=historical_count,
        current_legacy_target_count=current_count,
    )


def load_retirement_manifest(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RetirementManifestError(f"cannot load retirement manifest: {error}") from error
    if not isinstance(payload, dict):
        raise RetirementManifestError("retirement manifest must be a JSON object")
    return payload


def write_retirement_manifest(path: str | Path, manifest: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "FORBIDDEN_POOL_SEGMENTS",
    "LEGACY_ROOTS",
    "RetirementFinding",
    "RetirementManifestError",
    "RetirementVerification",
    "freeze_retirement_manifest",
    "list_git_files",
    "load_retirement_manifest",
    "payload_digest",
    "read_git_blobs",
    "resolve_revision",
    "revision_tree",
    "verify_retirement_manifest",
    "write_retirement_manifest",
]
