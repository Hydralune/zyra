from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import (  # noqa: E402
    InternalizationLedger,
    InternalizationLedgerEntry,
)
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


PRIOR_PATH = ROOT / "scripts" / "sync_m2_worker_causal_timeline_source_ledger.py"
PRIOR_SPEC = importlib.util.spec_from_file_location(
    "m2_artifact_ledger_helpers",
    PRIOR_PATH,
)
if PRIOR_SPEC is None or PRIOR_SPEC.loader is None:
    raise RuntimeError("M2 ledger helper could not be loaded")
PRIOR = importlib.util.module_from_spec(PRIOR_SPEC)
PRIOR_SPEC.loader.exec_module(PRIOR)

DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M2-S03A-01"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "9ca93e3"
BASELINE_COMMIT = "1833319acdb8a09fac3438b12356da0e9c78d6bb"
STAMP = "2026-07-24T00:00:00.000Z"
WEB_TEST = "apps/web/test/artifact-custody-catalog-viewers.test.ts"
INTEGRATION_TEST = "tests/integration/test_artifact_custody_catalog_viewers.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/app/src/context/file/{content-cache.ts,view-cache.ts};"
            "packages/app/src/pages/session/{file-tabs.tsx,"
            "v2/session-file-list-v2.tsx}"
        ),
        "capability_name": "bounded_artifact_catalog_content_and_view_state",
        "capability_summary": (
            "Byte-accounted cache, stable immutable selection, bounded range "
            "loading, explicit viewer lifecycle, virtualization and search."
        ),
        "targets": [
            "apps/web/src/features/artifacts/cache.ts",
            "apps/web/src/features/artifacts/catalog.ts",
            "apps/web/src/features/artifacts/content.ts",
            "apps/web/src/features/artifacts/runtime.ts",
            "apps/web/src/features/artifacts/search-index.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode file cache, stable selection and file-view lifecycle "
            "mechanics were cropped into Zyra artifact modules over typed "
            "task-scoped APIs. OpenCode session, store and provider owners "
            "were not copied."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "frontend/src/components/features/files/{file-list,file-item}.tsx;"
            "frontend/src/components/features/markdown/"
            "{markdown-renderer,anchor,code,table}.tsx"
        ),
        "capability_name": "safe_artifact_viewers_and_file_interaction",
        "capability_summary": (
            "Safe Markdown tokenization, non-executable links, explicit file "
            "interaction and content-security presentation boundaries."
        ),
        "targets": [
            "apps/web/src/features/artifacts/security.ts",
            "apps/web/src/features/artifacts/viewers.ts",
            "apps/web/src/features/artifacts/view/artifact-workbench.tsx",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "selective_port",
        "rationale": (
            "Only file interaction and safe Markdown element mechanics "
            "supplement the primary browser runtime. OpenHands conversation, "
            "Redux, sandbox and server owners remain excluded."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "python",
        "target_language": "python",
        "source_path": (
            "openhands/app_server/file_store/files.py::write_from_path;"
            "openhands/app_server/file_store/local.py::{write,write_from_path}"
        ),
        "capability_name": "atomic_artifact_custody_and_streamed_read_gate",
        "capability_summary": (
            "Atomic byte custody, stream-oriented hashing/copying and "
            "task-scoped bounded file reads integrated with Zyra metadata."
        ),
        "targets": [
            "packages/runtime/zyra_runtime/artifacts.py",
            "apps/api/zyra_api/artifact_api.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration/canonical_owner_integration",
        "migration_strategy": "selective_port",
        "rationale": (
            "Atomic and streaming mechanics were integrated inside the "
            "existing LocalArtifactStore and task API. The Zyra TaskStore and "
            "LocalArtifactStore remain canonical; no OpenHands runtime or "
            "filesystem owner is invoked."
        ),
    },
    {
        "source_repo": "agent-framework",
        "source_commit": "d50698bb797710bfd1ebf34eb621c905a4009b2d",
        "source_language": "python",
        "target_language": "none",
        "source_path": (
            "python/packages/ag-ui/agent_framework_ag_ui/"
            "{_agent.py,_agent_run.py} event/history contracts"
        ),
        "capability_name": "artifact_reference_lifecycle_conformance",
        "capability_summary": (
            "Event/history ordering and run-lifecycle artifact reference "
            "conformance without production ownership."
        ),
        "targets": [WEB_TEST, INTEGRATION_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "Agent Framework is behavior comparison only and contributes no "
            "artifact store, event reducer, workflow or viewer runtime."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "none",
        "source_path": "packages/hashline/src/apply.ts",
        "capability_name": "hash_bound_artifact_receipt_conformance",
        "capability_summary": (
            "Exact-file/version receipt discipline used only for immutable "
            "read-receipt negative tests before the later PatchEngine slice."
        ),
        "targets": [WEB_TEST, INTEGRATION_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "OMP hashline vocabulary is conformance only. No patch, apply, "
            "review, file mutation or receipt owner is migrated in this slice."
        ),
    },
)


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    value = PRIOR.entry(decision)
    source_role = str(decision["source_role"])
    production = source_role in {
        "primary_implementation",
        "supplementary_implementation",
    }
    value.update(
        {
            "owner_unit": OWNER_UNIT,
            "created_at": STAMP,
            "updated_at": STAMP,
            "downstream_units": ["M2-S03A-02", "M2-03B", "M2-04A"],
            "replacement_plan": (
                "Replace behind ArtifactWorkbenchRuntime or "
                "ArtifactCatalogService without moving TaskStore or "
                "LocalArtifactStore custody."
                if production
                else f"{source_role} source; no production owner is selected."
            ),
            "risk_notes": [
                "No runtime path depends on a parent source repository.",
                (
                    "TaskStore ArtifactRef and LocalArtifactStore remain the "
                    "only canonical metadata and byte owners."
                ),
                (
                    "Browser cache, bookmarks, selection and media URLs are "
                    "bounded projections and never artifact truth."
                ),
                (
                    "This slice owns no PatchEngine, diff mutation or review "
                    "receipt state."
                ),
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 artifact catalog/viewer source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["tags"] = ["m2-03a", source_role]
    value["main_path"] = {
        "surfaces": ["web_workbench", "task_detail"],
        "event_types": ["artifact.*", "tool.*", "topology.*", "timeline.*"],
        "api_routes": [
            "/tasks/{task_id}/artifacts",
            "/tasks/{task_id}/artifacts/{artifact_id}",
            "/tasks/{task_id}/artifacts/{artifact_id}/content",
            "/tasks/{task_id}/artifacts/{artifact_id}/download",
            "/tasks/{task_id}/artifacts/{artifact_id}/receipts",
        ],
        "control_commands": [],
        "artifact_kinds": [
            "text",
            "markdown",
            "structured_data",
            "image",
            "audio",
            "video",
            "file",
        ],
        "worker_runtime": (
            "TaskStore ArtifactRef -> ArtifactCatalogService -> "
            "LocalArtifactStore -> typed TaskApi -> "
            "ArtifactWorkbenchRuntime -> policy-selected viewer"
            if production
            else f"{source_role}; behavior only"
        ),
        "ui_panels": [
            "artifact_catalog",
            "artifact_viewer",
            "artifact_bookmarks",
        ],
    }
    value["runtime_entry"] = {
        "module": (
            "apps.web.src.features.artifacts.runtime"
            if production
            else "apps.web.test.artifact-custody-catalog-viewers"
        ),
        "function": (
            "ArtifactWorkbenchRuntime/ArtifactCatalogService"
            if production
            else "artifact conformance tests"
        ),
        "protocol": "zyra.artifact.v2",
        "health_check": (
            f"bun test ./{WEB_TEST}; "
            f"python -m pytest -q {INTEGRATION_TEST}"
        ),
        "command": "bun run build:web" if production else "",
        "config_refs": list(decision["targets"]),
        "environment_refs": ["ZYRA_ARTIFACT_ROOT"] if production else [],
    }
    value["test_entries"] = [
        {
            "path": WEB_TEST,
            "command": f"bun test ./{WEB_TEST}",
            "kind": "unit",
            "expected_signal": (
                "strict contracts, double redaction, bounded cache/catalog/"
                "search, safe viewers, media hash verification, audit chain, "
                "disconnect and disabled admission"
            ),
            "required": True,
        },
        {
            "path": INTEGRATION_TEST,
            "command": f"python -m pytest -q {INTEGRATION_TEST}",
            "kind": "integration",
            "expected_signal": (
                "real files and task-scoped HTTP prove digest/revision custody, "
                "range reads, cursor filters, policy denial and owner disable"
            ),
            "required": production,
        },
    ]
    value["tags"] = [
        "m2-03a",
        SLICE_ID.lower(),
        "artifact-custody",
        "artifact-viewer",
        source_role,
    ]
    value["metadata"].update(
        {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_artifact_reference_owner": "python.SQLiteStore.TaskState",
            "canonical_artifact_byte_owner": "python.LocalArtifactStore",
            "canonical_artifact_api_owner": "python.ArtifactCatalogService",
            "transient_view_owner": "typescript.ArtifactWorkbenchRuntime",
            "transient_cache_owner": "typescript.ArtifactContentCache",
            "root_source_runtime_dependency": False,
            "implementation_commit": IMPLEMENTATION_COMMIT,
            "baseline_commit": BASELINE_COMMIT,
            "rationale": decision["rationale"],
        }
    )
    return value


def rewrite(document: Any) -> Any:
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("ledger seed must be a list or contain entries")
    replacements = [entry(decision) for decision in DECISIONS]
    output: list[dict[str, Any]] = []
    inserted = False
    for item in entries:
        owned = (
            str(item.get("owner_unit") or "") == OWNER_UNIT
            or str((item.get("metadata") or {}).get("slice_id") or "")
            == SLICE_ID
        )
        if owned:
            if not inserted:
                output.extend(replacements)
                inserted = True
            continue
        output.append(item)
    if not inserted:
        output.extend(replacements)
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    if isinstance(document, list):
        return output
    return {
        **document,
        "entries": output,
        "summary": to_jsonable(InternalizationLedger(typed).summary()),
    }


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def git_file_exists(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def audit_targets(document: Any) -> tuple[int, list[str]]:
    entries = document if isinstance(document, list) else document["entries"]
    selected = [
        item
        for item in entries
        if str(item.get("owner_unit") or "") == OWNER_UNIT
    ]
    errors: list[str] = []
    for item in selected:
        identity = str(item.get("ledger_id") or "")
        for binding in item.get("target_bindings") or []:
            path = str(binding.get("target_path") or "")
            if path and not git_file_exists(IMPLEMENTATION_COMMIT, path):
                errors.append(f"{identity}:missing:{path}")
    return len(selected), errors


def synchronize(path: Path, *, write: bool) -> tuple[bool, int, list[str]]:
    current = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite(current)
    aligned = canonical(current) == canonical(expected)
    if write and not aligned:
        path.write_text(canonical(expected), encoding="utf-8", newline="\n")
        current = expected
        aligned = True
    checked = current if aligned else expected
    count, errors = audit_targets(checked)
    return aligned, count, errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    aligned, count, errors = synchronize(
        args.ledger.resolve(),
        write=args.write or not args.check,
    )
    print(f"m2_artifact_viewer_ledger_aligned={str(aligned).lower()}")
    print(f"m2_artifact_viewer_source_decision_count={len(DECISIONS)}")
    print(f"m2_artifact_viewer_ledger_entry_count={count}")
    print(f"m2_artifact_viewer_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
