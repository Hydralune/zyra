from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package_path in (PROJECT_ROOT / "packages" / "core", PROJECT_ROOT / "packages" / "integrations"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import (  # noqa: E402
    InternalizationLedger,
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    SourceEvidence,
    TargetBinding,
    TestEntry,
)
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


LEDGER_PATH = PROJECT_ROOT / "packages" / "integrations" / "zyra_integrations" / "data" / "internalization_ledger_seed.json"
OWNER_UNIT = "M1-04C"
SLICE_ID = "M1-S04C-01"
STAMP = "2026-07-13T12:00:00.000Z"
BEHAVIOR_TEST = "tests/unit/test_browser_action_registry_permission_foundation.py"
BEHAVIOR_COMMAND = "python -m unittest tests.unit.test_browser_action_registry_permission_foundation"


class Disposition(StrEnum):
    MIGRATED = "zyra_module_migrated"
    SUPPLEMENT = "supplementary_existing_owner_bridge"
    CONFORMANCE = "conformance_only"
    REFERENCE = "reference_only"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Decision:
    repo: str
    source_path: str
    targets: tuple[str, ...]
    capability: str
    disposition: Disposition
    role: str
    strategy: MigrationStrategy
    rationale: str
    source_graph_ref: str

    @property
    def productized(self) -> bool:
        return self.disposition in {Disposition.MIGRATED, Disposition.SUPPLEMENT}


DECISIONS = (
    Decision(
        "browser-use",
        "browser_use/tools/registry/service.py;browser_use/tools/registry/views.py;browser_use/tools/views.py",
        (
            "packages/workers/zyra_workers/browser_action/registry.py",
            "packages/workers/zyra_workers/browser_action/schema.py",
            "packages/workers/zyra_workers/browser_action/catalog.py",
        ),
        "browser action immutable registry and strict ToolSpec projection",
        Disposition.MIGRATED,
        "primary",
        MigrationStrategy.DIRECT_PORT,
        "Runtime reflection and vendor AST discovery were replaced by a closed Zyra-owned catalog.",
        "source-graphs/browser-use/batch-04-tools-security-filesystem.md",
    ),
    Decision(
        "browser-use",
        "browser_use/tools/service.py",
        (
            "packages/workers/zyra_workers/browser_action/gateway.py",
            "packages/workers/zyra_workers/browser_action/executor.py",
            "packages/workers/zyra_workers/browser_action/sequence.py",
        ),
        "typed action gateway, side-effect fence and all-actions-first preflight",
        Disposition.MIGRATED,
        "primary",
        MigrationStrategy.REIMPLEMENTED_PATTERN,
        "Action branches were decomposed around Zyra permission, selector, event and artifact owners.",
        "source-graphs/browser-use/batch-04-tools-security-filesystem.md",
    ),
    Decision(
        "browser-use",
        "browser_use/actor/page.py;browser_use/actor/element.py;browser_use/actor/mouse.py;browser_use/actor/utils.py;browser_use/browser/watchdogs/default_action_watchdog.py",
        (
            "packages/workers/zyra_workers/browser_action/cdp_probe.py",
            "packages/workers/zyra_workers/browser_action/geometry_guard.py",
            "packages/workers/zyra_workers/browser_action/keyboard_codec.py",
            "packages/workers/zyra_workers/browser_action/executor.py",
        ),
        "frame-correct CDP geometry, occlusion, scroll, pointer and keyboard mechanics",
        Disposition.MIGRATED,
        "primary",
        MigrationStrategy.DIRECT_PORT,
        "Blind JavaScript click, nearest-element fallback and public actor bypasses were removed.",
        "source-graphs/browser-use/batch-04-tools-security-filesystem.md",
    ),
    Decision(
        "browser-use",
        "browser_use/filesystem/file_system.py;browser_use/browser/watchdogs/downloads_watchdog.py",
        (
            "packages/workers/zyra_workers/browser_action/file_policy.py",
            "packages/workers/zyra_workers/browser_action/download_guard.py",
        ),
        "upload identity/containment and grant-scoped download quarantine",
        Disposition.MIGRATED,
        "primary",
        MigrationStrategy.REIMPLEMENTED_PATTERN,
        "Zyra workspace/artifact roots replace the upstream parallel filesystem state owner.",
        "source-graphs/browser-use/batch-04-tools-security-filesystem.md",
    ),
    Decision(
        "browser-use",
        "browser_use/browser/watchdogs/security_watchdog.py",
        (
            "packages/workers/zyra_workers/browser_action/network_policy.py",
            "packages/workers/zyra_workers/browser_action/redirect_guard.py",
        ),
        "canonical URL/domain/DNS-IP policy and pre-network redirect interception",
        Disposition.MIGRATED,
        "primary",
        MigrationStrategy.REIMPLEMENTED_PATTERN,
        "Post-navigation observation was strengthened with DNS pinning and request-paused denial.",
        "source-graphs/browser-use/batch-04-tools-security-filesystem.md",
    ),
    Decision(
        "claude-code-best",
        "src/cli/src/utils/permissions/*;src/QueryEngine.ts;src/tools/*",
        (
            "packages/workers/zyra_workers/browser_action/permission_bridge.py",
            "packages/runtime/zyra_runtime/permission/action_gate.py",
        ),
        "exact browser action permission bridge to the existing one-use grant owner",
        Disposition.SUPPLEMENT,
        "supplementary",
        MigrationStrategy.ADAPTER,
        "M1-03A remains the only decision/request/grant owner; 04C contributes browser binding material.",
        "source-graphs/claude-code-best/batch-03-permission-runtime.md",
    ),
    Decision(
        "oh-my-pi",
        "packages/coding-agent/src/tools/**;packages/hashline/**",
        (
            "packages/workers/zyra_workers/browser_action/hook_preflight.py",
            "packages/workers/zyra_workers/browser_action/sequence.py",
        ),
        "deny-first typed hooks, argument hashes and all-sections preflight",
        Disposition.MIGRATED,
        "supplementary",
        MigrationStrategy.REIMPLEMENTED_PATTERN,
        "Dynamic extension execution and yolo/bypass semantics are intentionally absent.",
        "source-graphs/oh-my-pi/batch-02-tool-runtime-permission-hooks.md",
    ),
    Decision(
        "opencode",
        "packages/opencode/src/permission/**",
        ("packages/workers/zyra_workers/browser_action/sensitive_policy.py",),
        "permission question behavior comparison",
        Disposition.CONFORMANCE,
        "conformance_only",
        MigrationStrategy.NOT_SELECTED,
        "Used only to compare ASK presentation; it cannot become a parallel BrowserPermissionRuntime.",
        "source-graphs/opencode/batch-05-tools-permissions-mcp-skills.md",
    ),
    Decision(
        "openclaw",
        "src/security/**;src/agents/tools/**",
        ("packages/workers/zyra_workers/browser_action/sensitive_policy.py",),
        "browser policy and safe-bin reference",
        Disposition.REFERENCE,
        "reference_only",
        MigrationStrategy.NOT_SELECTED,
        "No production migration quota; deterministic browser policy remains Zyra-owned.",
        "source-graphs/openclaw/batch-03-tools-policy-exec-approval.md",
    ),
    Decision(
        "agentscope",
        "src/agentscope/tool/**/permission*",
        ("packages/workers/zyra_workers/browser_action/sensitive_policy.py",),
        "permission engine reference",
        Disposition.REFERENCE,
        "reference_only",
        MigrationStrategy.NOT_SELECTED,
        "The overlapping permission state machine is rejected to preserve the M1-03A owner.",
        "source-graphs/agentscope/batch-06-runtime-tools-memory.md",
    ),
)


def build_entry(decision: Decision) -> InternalizationLedgerEntry:
    prototype = InternalizationLedgerEntry.new(
        source_repo=decision.repo,
        source_path=decision.source_path,
        capability_name=decision.capability,
        capability_summary=decision.rationale,
        target_paths=[decision.targets[0]],
    )
    entry = InternalizationLedgerEntry(
        ledger_id=prototype.ledger_id,
        source_repo=decision.repo,
        source_path=decision.source_path,
        capability_name=decision.capability,
        capability_summary=decision.rationale,
        target_bindings=[
            TargetBinding(path, role="primary" if index == 0 else "supporting", required_for_main_path=decision.productized)
            for index, path in enumerate(decision.targets)
        ],
        migration_strategy=decision.strategy,
        main_path_status=MainPathStatus.TESTED_MAIN_PATH if decision.productized else MainPathStatus.INVENTORIED,
        lifecycle=LedgerLifecycle.PRODUCTIZED if decision.productized else LedgerLifecycle.CANDIDATE,
        runtime_entry=RuntimeEntry(
            module="zyra_workers.browser_action.gateway",
            function="BrowserActionGateway.run" if decision.productized else "BrowserActionSourceAuditor.audit",
            protocol="zyra-browser-action-v1",
            health_check=BEHAVIOR_COMMAND,
            config_refs=list(decision.targets),
        ),
        test_entries=[TestEntry(
            path=BEHAVIOR_TEST,
            command=BEHAVIOR_COMMAND,
            kind="integration",
            expected_signal="real 03A grant changes CDP reachability; denied/stale paths have zero browser, network and file effects",
            required=True,
        )],
        main_path=MainPathBinding(
            surfaces=["browser_worker", "browser_action_gateway", "permission_runtime", "event_log", "artifact_store"] if decision.productized else ["source_conformance"],
            event_types=["agent_message", "artifact_written", "recovery_planned"] if decision.productized else [],
            artifact_kinds=["structured_data", "screenshot", "file"] if decision.productized else [],
            worker_runtime="BrowserWorkerRuntime registry -> BrowserActionGateway -> 03A -> side-effect fence" if decision.productized else decision.disposition.value,
        ),
        line_count_policy=LineCountPolicy.COUNTS_WHEN_PRODUCTIZED if decision.productized else LineCountPolicy.EXCLUDED_INVENTORY_ONLY,
        license_notice=LicenseNotice(
            source_repo=decision.repo,
            status=NoticeStatus.RECORDED,
            license_hint="Source mechanism recorded for Zyra-owned browser action internalization.",
            notice_path="third_party/NOTICE.md",
            notes="No runtime dependency on a root source repository.",
        ),
        owner_unit=OWNER_UNIT,
        milestone="M1",
        downstream_units=["M1-S04C-02", "M1-04D", "M2-01A"],
        dependencies=["M1-03A", "M1-04A", "M1-04B"] if decision.productized else [],
        source_evidence=[SourceEvidence(
            source_repo=decision.repo,
            source_path=decision.source_path,
            exists_in_workspace=True,
            source_kind="source_graph_decision",
            reason=f"{SLICE_ID} source-to-target decision",
            symbols=[],
            tags=["browser-action", decision.role, decision.disposition.value],
        )],
        tags=["m1-04c", "slice-04c-01", "browser-action", decision.role, decision.disposition.value],
        blockers=[],
        risk_notes=[decision.rationale],
        replacement_plan=decision.rationale,
        created_at=STAMP,
        updated_at=STAMP,
        metadata={
            "slice_id": SLICE_ID,
            "source_role": decision.role,
            "source_disposition": decision.disposition.value,
            "source_graph_ref": decision.source_graph_ref,
        },
    )
    errors = entry.validate()
    if errors:
        raise ValueError(f"invalid browser action ledger entry {entry.ledger_id}: {errors}")
    return entry


def rewrite(payload: dict[str, object]) -> dict[str, object]:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("ledger payload has no entries list")
    replacements = [build_entry(decision).to_dict() for decision in DECISIONS]
    output = [item for item in raw_entries if isinstance(item, dict) and item.get("owner_unit") != OWNER_UNIT]
    output.extend(replacements)
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    result = dict(payload)
    result["entries"] = output
    result["summary"] = to_jsonable(InternalizationLedger(typed).summary())
    return result


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    current = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite(current)
    aligned = canonical(current) == canonical(expected)
    if write and not aligned:
        path.write_text(canonical(expected), encoding="utf-8", newline="\n")
        aligned = True
    return aligned, len(DECISIONS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize M1-S04C-01 browser action source decisions.")
    parser.add_argument("--ledger-path", type=Path, default=LEDGER_PATH)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    aligned, count = synchronize(args.ledger_path.resolve(), write=args.write)
    print(f"browser_action_source_ledger_aligned={str(aligned).lower()}")
    print(f"browser_action_source_decision_count={count}")
    print(f"browser_action_productized_count={sum(item.productized for item in DECISIONS)}")
    print(f"browser_action_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={args.ledger_path.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
