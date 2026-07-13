from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    PROJECT_ROOT / "packages" / "core",
    PROJECT_ROOT / "packages" / "integrations",
    PROJECT_ROOT / "packages" / "runtime",
):
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


DEFAULT_LEDGER_PATH = (
    PROJECT_ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M1-04B"
SLICE_ID = "M1-S04B-01"
STAMP = "2026-07-13T06:00:00.000Z"
SOURCE_REPO = "browser-use"
SOURCE_GRAPH_REF = "source-graphs/browser-use/batch-03-dom-state-serializer-extraction.md"
BEHAVIOR_TEST = "tests/integration/test_browser_message_state_compression_foundation.py"
BEHAVIOR_COMMAND = (
    "python -m unittest "
    "tests.integration.test_browser_message_state_compression_foundation"
)


class SourceDisposition(StrEnum):
    MIGRATED = "zyra_module_migrated"
    CONFORMANCE_ONLY = "conformance_only"
    EXPERIMENTAL = "experimental"
    REFERENCE_ONLY = "reference_only"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class BrowserMessageStateSourceDecision:
    source_path: str
    target_paths: tuple[str, ...]
    mechanisms: tuple[str, ...]
    disposition: SourceDisposition
    strategy: MigrationStrategy = MigrationStrategy.DIRECT_PORT
    rationale: str = ""
    next_owner: str = ""
    source_repo: str = SOURCE_REPO
    source_role: str = "primary"
    source_graph_ref: str = SOURCE_GRAPH_REF
    slice_id: str = SLICE_ID
    behavior_test: str = BEHAVIOR_TEST
    behavior_command: str = BEHAVIOR_COMMAND

    @property
    def claims_runtime_ownership(self) -> bool:
        return self.disposition is SourceDisposition.MIGRATED


SOURCE_DECISIONS = (
    BrowserMessageStateSourceDecision(
        "browser_use/screenshots/__init__.py",
        ("packages/workers/zyra_workers/browser_state/artifact_serializer.py",),
        ("screenshot reference surface",),
        SourceDisposition.REFERENCE_ONLY,
        MigrationStrategy.NOT_SELECTED,
        "The package initializer contributes no state algorithm; 04B preserves existing 04A screenshot artifact references.",
        "M1-S04C-01",
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/serializer/clickable_elements.py",
        (
            "packages/workers/zyra_workers/browser_state/interactivity.py",
            "packages/workers/zyra_workers/browser_state/selector_ranker.py",
        ),
        ("interactive element classification", "selector disclosure ranking"),
        SourceDisposition.MIGRATED,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/enhanced_snapshot.py",
        (
            "packages/workers/zyra_workers/browser_state/snapshot_decoder.py",
            "packages/workers/zyra_workers/browser_state/dom_builder.py",
            "packages/workers/zyra_workers/browser_state/runtime.py",
        ),
        ("DOMSnapshot decoding", "DOM and AX merge", "frame-aware capture identity"),
        SourceDisposition.MIGRATED,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/serializer/eval_serializer.py",
        ("packages/workers/zyra_workers/browser_state/serializer.py",),
        ("evaluate-based DOM serialization alternative",),
        SourceDisposition.REFERENCE_ONLY,
        MigrationStrategy.NOT_SELECTED,
        "The evaluate serializer was rejected for the authoritative path; Zyra requires CDP DOMSnapshot plus DOM and AX capture.",
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/playground/extraction.py",
        ("packages/workers/zyra_workers/browser_state/semantic_sections.py",),
        ("experimental extraction examples",),
        SourceDisposition.REFERENCE_ONLY,
        MigrationStrategy.NOT_SELECTED,
        "Playground extraction is not a product runtime dependency; semantic extraction is deterministic Zyra code.",
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/serializer/html_serializer.py",
        ("packages/workers/zyra_workers/browser_state/serializer.py",),
        ("readable HTML serialization", "attribute filtering"),
        SourceDisposition.MIGRATED,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/markdown_extractor.py",
        ("packages/workers/zyra_workers/browser_state/semantic_sections.py",),
        ("semantic section extraction", "low-entropy document outline"),
        SourceDisposition.MIGRATED,
        MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/playground/multi_act.py",
        ("packages/workers/zyra_workers/browser_state/watchdog.py",),
        ("multi-action playground",),
        SourceDisposition.DEFERRED,
        MigrationStrategy.PLANNED_ADAPTER,
        "Multi-action registry execution belongs to the action/controller watchdog slice, not state projection.",
        "M1-S04D-01",
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/serializer/paint_order.py",
        (
            "packages/workers/zyra_workers/browser_state/occlusion.py",
            "packages/workers/zyra_workers/browser_state/serializer.py",
        ),
        ("paint-order visibility", "occlusion-aware serialization"),
        SourceDisposition.MIGRATED,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/serializer/serializer.py",
        (
            "packages/workers/zyra_workers/browser_state/models.py",
            "packages/workers/zyra_workers/browser_state/serializer.py",
            "packages/workers/zyra_workers/browser_state/dom_builder.py",
        ),
        ("DOM tree model", "serializer pipeline", "selector identity"),
        SourceDisposition.MIGRATED,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/agent/message_manager/service.py",
        (
            "packages/workers/zyra_workers/browser_context/message_manager.py",
            "packages/workers/zyra_workers/browser_context/compressor.py",
            "packages/workers/zyra_workers/browser_context/application.py",
            "packages/workers/zyra_workers/browser_context/task_integration.py",
            "packages/workers/zyra_workers/browser_context/api_projection.py",
        ),
        (
            "message projection",
            "context compression",
            "turn application service",
            "read-once task checkpoint and provider delivery",
        ),
        SourceDisposition.MIGRATED,
        MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/service.py",
        (
            "packages/workers/zyra_workers/browser_state/runtime.py",
            "packages/workers/zyra_workers/browser_state/capture_policy.py",
        ),
        ("authoritative DOM state service", "capture policy"),
        SourceDisposition.MIGRATED,
        MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/screenshots/service.py",
        (
            "packages/workers/zyra_workers/browser_state/artifact_serializer.py",
            "packages/workers/zyra_workers/browser_context/externalizer.py",
        ),
        ("screenshot artifact candidate",),
        SourceDisposition.REFERENCE_ONLY,
        MigrationStrategy.NOT_SELECTED,
        "Screenshot acquisition remains owned by the 04A browser session; 04B only preserves and externalizes artifact references.",
        "M1-S04C-01",
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/agent/message_manager/utils.py",
        (
            "packages/workers/zyra_workers/browser_context/action_result.py",
            "packages/workers/zyra_workers/browser_context/history.py",
        ),
        ("action result normalization", "atomic tool pair history"),
        SourceDisposition.MIGRATED,
        MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/utils.py",
        ("packages/workers/zyra_workers/browser_state/text.py",),
        ("DOM text normalization", "fact identity"),
        SourceDisposition.MIGRATED,
        MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/agent/message_manager/views.py",
        (
            "packages/workers/zyra_workers/browser_context/models.py",
            "packages/workers/zyra_workers/browser_context/history.py",
        ),
        ("message history contracts", "projection models"),
        SourceDisposition.MIGRATED,
        MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/dom/views.py",
        (
            "packages/workers/zyra_workers/browser_state/models.py",
            "packages/workers/zyra_workers/browser_state/contracts.py",
        ),
        ("DOM node contracts", "selector map revision contracts"),
        SourceDisposition.MIGRATED,
    ),
    BrowserMessageStateSourceDecision(
        "browser_use/browser/watchdogs/dom_watchdog.py",
        (
            "packages/workers/zyra_workers/browser_state/frame_capture.py",
            "packages/workers/zyra_workers/browser_state/selector_probe.py",
        ),
        (
            "same-origin and OOPIF target/session capture",
            "live backend-node generation and focus guard",
        ),
        SourceDisposition.MIGRATED,
        slice_id="M1-S04B-02",
        behavior_test="tests/integration/test_browser_message_state_compression_integration.py",
        behavior_command=(
            "python -m pytest -p no:cacheprovider "
            "tests/integration/test_browser_message_state_compression_integration.py"
        ),
        strategy=MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "src/query.ts",
        (
            "packages/workers/zyra_workers/browser_context/task_integration.py",
            "packages/workers/zyra_workers/code_worker_runtime.py",
            "apps/api/zyra_api/main.py",
        ),
        (
            "provider-envelope context selection",
            "tool result budget custody",
            "checkpointed compact/restore boundary",
        ),
        SourceDisposition.MIGRATED,
        source_repo="claude-code-best",
        source_role="supplementary",
        source_graph_ref="source-graphs/claude-code-best/batch-02-query-tool-loop.md",
        slice_id="M1-S04B-02",
        behavior_test="tests/integration/test_browser_message_state_compression_integration.py",
        behavior_command=(
            "python -m pytest -p no:cacheprovider "
            "tests/integration/test_browser_message_state_compression_integration.py"
        ),
        strategy=MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "packages/agent/src/agent-loop.ts",
        (
            "packages/workers/zyra_workers/browser_context/action_envelope.py",
            "packages/workers/zyra_workers/browser_context/causal_runtime.py",
        ),
        (
            "malformed and partial tool result normalization",
            "atomic action/result causation",
            "oversize result artifact handoff",
        ),
        SourceDisposition.MIGRATED,
        source_repo="oh-my-pi",
        source_role="supplementary",
        source_graph_ref="source-graphs/oh-my-pi/batch-01-agent-loop-session.md",
        slice_id="M1-S04B-02",
        behavior_test="tests/integration/test_browser_message_state_compression_integration.py",
        behavior_command=(
            "python -m pytest -p no:cacheprovider "
            "tests/integration/test_browser_message_state_compression_integration.py"
        ),
        strategy=MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "packages/agent/src/append-only-context.ts",
        ("packages/workers/zyra_workers/browser_context/task_integration.py",),
        ("append-only typed browser disclosure delivery", "read-once provider selection"),
        SourceDisposition.MIGRATED,
        source_repo="oh-my-pi",
        source_role="supplementary",
        source_graph_ref="source-graphs/oh-my-pi/batch-01-agent-loop-session.md",
        slice_id="M1-S04B-02",
        behavior_test="tests/integration/test_browser_message_state_compression_integration.py",
        behavior_command=(
            "python -m pytest -p no:cacheprovider "
            "tests/integration/test_browser_message_state_compression_integration.py"
        ),
        strategy=MigrationStrategy.REIMPLEMENTED_PATTERN,
    ),
    BrowserMessageStateSourceDecision(
        "packages/snapcompact/src/index.ts",
        ("packages/workers/zyra_workers/browser_context/ablation.py",),
        ("default-off bitmap compression fidelity ablation",),
        SourceDisposition.EXPERIMENTAL,
        source_repo="oh-my-pi",
        source_role="experimental",
        source_graph_ref="source-graphs/oh-my-pi/batch-04-memory-compaction.md",
        slice_id="M1-S04B-02",
        behavior_test="tests/integration/test_browser_message_state_compression_integration.py",
        behavior_command=(
            "python -m pytest -p no:cacheprovider "
            "tests/integration/test_browser_message_state_compression_integration.py"
        ),
        strategy=MigrationStrategy.NOT_SELECTED,
        rationale=(
            "Snapcompact remains default-off and non-authoritative; it only supplies the bounded "
            "bitmap comparison lane and cannot own DOM, selector, context, or restore state."
        ),
    ),
    BrowserMessageStateSourceDecision(
        "python/packages/core/agent_framework/_harness/_loop.py",
        ("tests/integration/test_browser_message_state_compression_integration.py",),
        ("fresh-context and session snapshot behavior comparison",),
        SourceDisposition.CONFORMANCE_ONLY,
        source_repo="agent-framework",
        source_role="conformance_only",
        source_graph_ref="source-graphs/agent-framework/batch-05-harness-control-memory.md",
        slice_id="M1-S04B-02",
        behavior_test="tests/integration/test_browser_message_state_compression_integration.py",
        behavior_command=(
            "python -m pytest -p no:cacheprovider "
            "tests/integration/test_browser_message_state_compression_integration.py"
        ),
        strategy=MigrationStrategy.NOT_SELECTED,
        rationale=(
            "Agent Framework remains a conformance oracle for session/history behavior; the protected "
            "02D context owner already supplies the selected canonical implementation."
        ),
    ),
    BrowserMessageStateSourceDecision(
        "packages/core/src/session/compaction.ts",
        ("tests/integration/test_browser_message_state_compression_integration.py",),
        ("session event and context epoch behavior comparison",),
        SourceDisposition.CONFORMANCE_ONLY,
        source_repo="opencode",
        source_role="conformance_only",
        source_graph_ref="source-graphs/opencode/source-graph.md",
        slice_id="M1-S04B-02",
        behavior_test="tests/integration/test_browser_message_state_compression_integration.py",
        behavior_command=(
            "python -m pytest -p no:cacheprovider "
            "tests/integration/test_browser_message_state_compression_integration.py"
        ),
        strategy=MigrationStrategy.NOT_SELECTED,
        rationale=(
            "opencode session/context epoch semantics are conformance-only here; adding its state model "
            "would duplicate TaskState and the M1-02D context checkpoint owner."
        ),
    ),
    BrowserMessageStateSourceDecision(
        "agent/context_compressor.py",
        ("tests/integration/test_browser_message_state_compression_integration.py",),
        ("long-session compression failure and anti-thrash comparison",),
        SourceDisposition.CONFORMANCE_ONLY,
        source_repo="hermes-agent",
        source_role="reference_only",
        source_graph_ref="source-graphs/hermes-agent/batch-03-session-memory-context-compression.md",
        slice_id="M1-S04B-02",
        behavior_test="tests/integration/test_browser_message_state_compression_integration.py",
        behavior_command=(
            "python -m pytest -p no:cacheprovider "
            "tests/integration/test_browser_message_state_compression_integration.py"
        ),
        strategy=MigrationStrategy.NOT_SELECTED,
        rationale=(
            "Hermes long-session compression remains reference-only for 04B; global compact locks, "
            "cooldowns, and memory ownership remain with M1-02D and the M1-06 units."
        ),
    ),
)


def _capability_name(decision: BrowserMessageStateSourceDecision) -> str:
    return f"message-manager-state: {Path(decision.source_path).stem}"


def _lifecycle(decision: BrowserMessageStateSourceDecision) -> LedgerLifecycle:
    if decision.disposition is SourceDisposition.MIGRATED:
        return LedgerLifecycle.PRODUCTIZED
    if decision.disposition is SourceDisposition.DEFERRED:
        return LedgerLifecycle.DEFERRED
    return LedgerLifecycle.CANDIDATE


def _status(decision: BrowserMessageStateSourceDecision) -> MainPathStatus:
    if decision.disposition is SourceDisposition.MIGRATED:
        return MainPathStatus.TESTED_MAIN_PATH
    if decision.disposition is SourceDisposition.DEFERRED:
        return MainPathStatus.PLANNED
    return MainPathStatus.INVENTORIED


def _runtime_entry(decision: BrowserMessageStateSourceDecision) -> RuntimeEntry:
    if decision.claims_runtime_ownership:
        return RuntimeEntry(
            module="zyra_workers.browser_worker",
            function="BrowserWorkerRuntime.run",
            protocol="zyra-browser-message-state-v1",
            health_check=decision.behavior_command,
            config_refs=[
                "packages/workers/zyra_workers/browser_context/application.py",
                "packages/workers/zyra_workers/browser_state/runtime.py",
            ],
        )
    return RuntimeEntry(
        module="zyra_workers.browser_context.application",
        function="BrowserMessageStateApplication.snapshot",
        protocol="zyra-browser-message-state-source-decision-v1",
        health_check="python scripts/sync_browser_message_state_source_ledger.py --check",
        config_refs=[decision.source_graph_ref],
    )


def _test_entry(decision: BrowserMessageStateSourceDecision) -> TestEntry:
    if decision.claims_runtime_ownership:
        return TestEntry(
            path=decision.behavior_test,
            command=decision.behavior_command,
            kind="integration",
            expected_signal=(
                "real BrowserWorkerRuntime capture changes events, artifacts, selector state, "
                "next context, and candidate memory"
            ),
            required=True,
        )
    return TestEntry(
        path="scripts/sync_browser_message_state_source_ledger.py",
        command="python scripts/sync_browser_message_state_source_ledger.py --check",
        kind="audit",
        expected_signal="the source disposition is explicit and cannot claim runtime ownership",
        required=True,
    )


def _main_path(decision: BrowserMessageStateSourceDecision) -> MainPathBinding:
    if not decision.claims_runtime_ownership:
        return MainPathBinding(
            surfaces=["browser_message_state_source_decision"],
            worker_runtime=(
                f"{decision.disposition.value}; behavior is owned by "
                f"{decision.next_owner or ', '.join(decision.target_paths)}"
            ),
        )
    return MainPathBinding(
        surfaces=[
            "browser_worker",
            "browser_dom_state",
            "browser_context_projection",
            "artifact_store",
            "memory_candidate_boundary",
        ],
        event_types=[
            "browser_session_lifecycle",
            "agent_message",
        ],
        artifact_kinds=["browser_dom_state", "browser_dom_html", "browser_fidelity_report"],
        worker_runtime="BrowserWorkerRuntime.run -> BrowserMessageStateApplication.capture_after_action",
    )


def _replacement_plan(decision: BrowserMessageStateSourceDecision) -> str:
    if decision.disposition is SourceDisposition.MIGRATED:
        return (
            "Selected mechanisms were decomposed into Zyra browser_state and browser_context modules. "
            "The runtime uses Zyra session, event, artifact, memory-candidate, compact, and failure boundaries "
            "without importing the parent browser-use repository."
        )
    if decision.disposition is SourceDisposition.DEFERRED:
        return f"Deferred to {decision.next_owner}: {decision.rationale}"
    return (
        f"Reference only: {decision.rationale} The source is not a runtime dependency and its lines are "
        "excluded from internalized production code."
    )


def build_entry(decision: BrowserMessageStateSourceDecision) -> InternalizationLedgerEntry:
    capability_name = _capability_name(decision)
    bindings = [
        TargetBinding(
            target_path=path,
            role="primary" if index == 0 else "supporting",
            required_for_main_path=decision.claims_runtime_ownership,
        )
        for index, path in enumerate(decision.target_paths)
    ]
    entry = InternalizationLedgerEntry(
        ledger_id=InternalizationLedgerEntry.new(
            source_repo=decision.source_repo,
            source_path=decision.source_path,
            capability_name=capability_name,
            capability_summary=", ".join(decision.mechanisms),
            target_paths=[decision.target_paths[0]],
        ).ledger_id,
        source_repo=decision.source_repo,
        source_path=decision.source_path,
        capability_name=capability_name,
        capability_summary=", ".join(decision.mechanisms),
        target_bindings=bindings,
        migration_strategy=decision.strategy,
        main_path_status=_status(decision),
        lifecycle=_lifecycle(decision),
        runtime_entry=_runtime_entry(decision),
        test_entries=[_test_entry(decision)],
        main_path=_main_path(decision),
        line_count_policy=(
            LineCountPolicy.COUNTS_WHEN_PRODUCTIZED
            if decision.claims_runtime_ownership
            else LineCountPolicy.EXCLUDED_INVENTORY_ONLY
        ),
        license_notice=LicenseNotice(
            source_repo=decision.source_repo,
            status=NoticeStatus.RECORDED,
            license_hint="Source mechanism recorded for Zyra-owned browser state internalization.",
            notice_path="third_party/NOTICE.md",
            notes="No runtime dependency on the parent source repository.",
        ),
        owner_unit=OWNER_UNIT,
        milestone="M1",
        downstream_units=["M1-04C", "M1-04D", "M1-06B", "M2-01A"],
        dependencies=["M1-04A", "M1-02D"] if decision.claims_runtime_ownership else [],
        source_evidence=[SourceEvidence(
            source_repo=decision.source_repo,
            source_path=decision.source_path,
            exists_in_workspace=True,
            source_kind="file",
            reason=f"{decision.slice_id} browser message/state source-to-target decision",
            symbols=[],
            tags=["browser-state", decision.disposition.value],
        )],
        tags=[
            "m1-04b",
            decision.slice_id.lower().replace("m1-s", "slice-"),
            "browser-message-state",
            decision.source_role,
            decision.disposition.value,
        ],
        blockers=[],
        risk_notes=([decision.rationale] if decision.rationale else []),
        replacement_plan=_replacement_plan(decision),
        created_at=STAMP,
        updated_at=STAMP,
        metadata={
            "source_disposition": decision.disposition.value,
            "source_graph_ref": decision.source_graph_ref,
            "source_role": decision.source_role,
            "next_owner": decision.next_owner,
            "slice_id": decision.slice_id,
        },
    )
    errors = entry.validate()
    if errors:
        raise ValueError(f"Invalid browser message-state ledger entry {entry.ledger_id}: {errors}")
    return entry


def expected_entries() -> list[InternalizationLedgerEntry]:
    entries = [build_entry(decision) for decision in SOURCE_DECISIONS]
    keys = {(entry.source_repo, entry.source_path) for entry in entries}
    if len(keys) != len(entries):
        raise ValueError("browser message-state decisions contain duplicate repository/path identities")
    return entries


def rewrite_payload(payload: dict[str, object]) -> dict[str, object]:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("ledger payload has no entries list")
    replacements = [entry.to_dict() for entry in expected_entries()]
    output: list[dict[str, object]] = []
    inserted = False
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("ledger entry is not an object")
        if raw.get("owner_unit") == OWNER_UNIT:
            if not inserted:
                output.extend(replacements)
                inserted = True
            continue
        output.append(raw)
    if not inserted:
        output.extend(replacements)
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    result = dict(payload)
    result["entries"] = output
    result["summary"] = to_jsonable(InternalizationLedger(typed).summary())
    return result


def canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int, int]:
    original = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite_payload(original)
    before = canonical_json(original)
    after = canonical_json(expected)
    aligned_before = before == after
    if write and not aligned_before:
        path.write_text(after, encoding="utf-8", newline="\n")
    return aligned_before if not write else True, len(expected_entries()), 0 if aligned_before else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize M1-04B browser message-state source decisions."
    )
    parser.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Write the deterministic M1-04B projection.")
    mode.add_argument("--check", action="store_true", help="Fail when M1-04B rows are not aligned.")
    mode.add_argument("--dry-run", action="store_true", help="Report drift without changing the ledger.")
    args = parser.parse_args(argv)
    path = args.ledger_path.resolve()
    aligned, count, changed = synchronize(path, write=args.write)
    migrated = sum(item.disposition is SourceDisposition.MIGRATED for item in SOURCE_DECISIONS)
    references = sum(item.disposition is SourceDisposition.REFERENCE_ONLY for item in SOURCE_DECISIONS)
    experimental = sum(item.disposition is SourceDisposition.EXPERIMENTAL for item in SOURCE_DECISIONS)
    conformance = sum(item.disposition is SourceDisposition.CONFORMANCE_ONLY for item in SOURCE_DECISIONS)
    deferred = sum(item.disposition is SourceDisposition.DEFERRED for item in SOURCE_DECISIONS)
    print(f"browser_message_state_source_ledger_aligned={str(aligned).lower()}")
    print(f"browser_message_state_source_decision_count={count}")
    print(f"browser_message_state_migrated_count={migrated}")
    print(f"browser_message_state_reference_count={references}")
    print(f"browser_message_state_experimental_count={experimental}")
    print(f"browser_message_state_conformance_count={conformance}")
    print(f"browser_message_state_deferred_count={deferred}")
    print(f"browser_message_state_owner_groups_changed={changed}")
    print(f"ledger_path={path}")
    if args.check:
        return 0 if aligned else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
