"""Evidence navigation and executable first-stage acceptance runbook."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from zyra_evaluation.freeze_reporting.canonical import digest

from .common import (
    FindingLedger,
    load_json,
    object_with_digest,
    require_boolean,
    require_choice,
    require_identity,
    require_mapping,
    require_sequence,
    require_text,
    safe_relative_path,
    stable_unique,
)


RUNBOOK_KINDS = (
    "default-entry",
    "semantic-health",
    "live-case",
    "ablation",
    "fault-recovery",
    "causal-trace",
    "final-artifact",
    "submission",
)


@dataclass(frozen=True, slots=True)
class RunbookEntry:
    """One stable navigation entry into product or acceptance evidence."""

    entry_id: str
    title: str
    kind: str
    path: str
    purpose: str
    acceptance: tuple[str, ...]
    requirement_ids: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    expected_result: str = ""
    required: bool = True

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "RunbookEntry":
        item = require_mapping(value, label)
        return cls(
            entry_id=require_identity(
                item.get("entry_id"),
                f"{label}.entry_id",
            ),
            title=require_text(item.get("title"), f"{label}.title"),
            kind=require_choice(
                item.get("kind"),
                f"{label}.kind",
                RUNBOOK_KINDS,
            ),
            path=safe_relative_path(item.get("path"), f"{label}.path"),
            purpose=require_text(
                item.get("purpose"),
                f"{label}.purpose",
                maximum=4096,
            ),
            acceptance=tuple(
                require_text(
                    entry,
                    f"{label}.acceptance[{index}]",
                    maximum=4096,
                )
                for index, entry in enumerate(
                    require_sequence(
                        item.get("acceptance"),
                        f"{label}.acceptance",
                        minimum=1,
                    )
                )
            ),
            requirement_ids=tuple(
                require_identity(
                    entry,
                    f"{label}.requirement_ids[{index}]",
                )
                for index, entry in enumerate(
                    require_sequence(
                        item.get("requirement_ids", []),
                        f"{label}.requirement_ids",
                        allow_empty=True,
                    )
                )
            ),
            command=tuple(
                require_text(
                    entry,
                    f"{label}.command[{index}]",
                    maximum=4096,
                )
                for index, entry in enumerate(
                    require_sequence(
                        item.get("command", []),
                        f"{label}.command",
                        allow_empty=True,
                    )
                )
            ),
            expected_result=require_text(
                item.get("expected_result", ""),
                f"{label}.expected_result",
                allow_empty=True,
                maximum=4096,
            ),
            required=require_boolean(
                item.get("required", True),
                f"{label}.required",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "title": self.title,
            "kind": self.kind,
            "path": self.path,
            "purpose": self.purpose,
            "acceptance": list(self.acceptance),
            "requirement_ids": list(self.requirement_ids),
            "command": list(self.command),
            "expected_result": self.expected_result,
            "required": self.required,
        }


class EvidenceNavigationBuilder:
    """Build a verifier-backed navigation index from S03-01 output."""

    REQUIRED_KINDS = {
        "default-entry",
        "semantic-health",
        "live-case",
        "ablation",
        "fault-recovery",
        "causal-trace",
        "final-artifact",
        "submission",
    }

    def __init__(
        self,
        repository_root: str | Path,
        freeze_output: str | Path,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.freeze_output = Path(freeze_output).resolve()

    def default_entries(self) -> list[RunbookEntry]:
        evidence_root = self.freeze_output.relative_to(
            self.repository_root
        ).as_posix()
        return [
            RunbookEntry(
                entry_id="product-entry",
                title="Start the governed product stack",
                kind="default-entry",
                path="README.md",
                purpose="Launch the default API and web product path.",
                acceptance=(
                    "default release configuration is used",
                    "no root source repository is required",
                ),
                command=("python", "scripts/verify_first_stage.py"),
                expected_result="final verifier returns valid=true",
            ),
            RunbookEntry(
                entry_id="semantic-health",
                title="Verify semantic release health",
                kind="semantic-health",
                path=(
                    "docs/reviews/evidence/M3-S02B-02/"
                    "verification-summary.json"
                ),
                purpose=(
                    "Inspect process, API, persistence, provider, and "
                    "artifact health rather than a port-only check."
                ),
                acceptance=(
                    "all 13 release gates pass",
                    "provider absence is fail-closed rather than simulated",
                ),
                requirement_ids=("REQ-COMP-001", "REQ-COMP-006"),
                expected_result="gate_count=13 and failed_gate_count=0",
            ),
            RunbookEntry(
                entry_id="live-cases",
                title="Inspect two cross-domain live cases",
                kind="live-case",
                path=f"{evidence_root}/generated/case-studies.json",
                purpose=(
                    "Validate independent seeded repetitions and useful "
                    "final artifacts for both required domains."
                ),
                acceptance=(
                    "at least two distinct domains exist",
                    "each domain has at least three independent repetitions",
                    "every run has at least 2,000 canonical transitions",
                    "human intervention count is zero",
                ),
                requirement_ids=(
                    "REQ-APP-001",
                    "REQ-APP-002",
                    "REQ-COMP-001",
                ),
                expected_result="six admitted live runs across two domains",
            ),
            RunbookEntry(
                entry_id="ablation",
                title="Compare dynamic and fixed-topology outcomes",
                kind="ablation",
                path=f"{evidence_root}/generated/ablation-material.json",
                purpose=(
                    "Show dynamic sparse topology and low-entropy behavior "
                    "against the fixed comparison."
                ),
                acceptance=(
                    "dynamic and fixed treatments share sealed inputs",
                    "reported deltas are backed by benchmark rows",
                ),
                requirement_ids=("REQ-TECH-001", "REQ-PERF-003"),
                expected_result="paired ablation table and topology evidence",
            ),
            RunbookEntry(
                entry_id="fault-recovery",
                title="Inspect injected failures and recovery",
                kind="fault-recovery",
                path=f"{evidence_root}/generated/case-studies.json",
                purpose=(
                    "Trace abnormal input, requirement change, node loss, "
                    "and provider failure through recovery and completion."
                ),
                acceptance=(
                    "fault event precedes recovery event",
                    "recovered run produces a final artifact",
                ),
                requirement_ids=("REQ-COMP-007", "REQ-TECH-002"),
                expected_result="fault-to-recovery causal chain is present",
            ),
            RunbookEntry(
                entry_id="causal-trace",
                title="Inspect replayable causal projections",
                kind="causal-trace",
                path=f"{evidence_root}/replay-verification.json",
                purpose=(
                    "Verify trace projections from immutable event and "
                    "checkpoint receipts."
                ),
                acceptance=(
                    "all declared projections replay",
                    "projection digests match evidence index references",
                ),
                requirement_ids=("REQ-COMP-005",),
                expected_result="all replay projections verify",
            ),
            RunbookEntry(
                entry_id="final-artifact",
                title="Inspect final artifact and evidence index",
                kind="final-artifact",
                path=(
                    f"{evidence_root}/generated/"
                    "100-point-evidence-index.json"
                ),
                purpose=(
                    "Navigate every requirement and scoring item from one "
                    "immutable index."
                ),
                acceptance=(
                    "all 19 required rows are present",
                    "score is exactly 100",
                ),
                requirement_ids=(
                    "REQ-APP-001",
                    "REQ-APP-002",
                    "REQ-APP-003",
                    "REQ-APP-004",
                    "REQ-APP-005",
                    "REQ-COMP-001",
                    "REQ-COMP-002",
                    "REQ-COMP-003",
                    "REQ-COMP-004",
                    "REQ-COMP-005",
                    "REQ-COMP-006",
                    "REQ-COMP-007",
                    "REQ-PERF-001",
                    "REQ-PERF-002",
                    "REQ-PERF-003",
                    "REQ-TECH-001",
                    "REQ-TECH-002",
                    "REQ-TECH-003",
                    "REQ-TECH-004",
                ),
                expected_result="requirement_count=19 and score=100",
            ),
            RunbookEntry(
                entry_id="submission",
                title="Verify the final submission candidate",
                kind="submission",
                path=(
                    "docs/reviews/evidence/M3-S03-02/"
                    "submission-verification.json"
                ),
                purpose=(
                    "Recompute manifest, archive, checksum, reviewer, and "
                    "email checklist bindings."
                ),
                acceptance=(
                    "independent submission verifier passes",
                    "two distinct reviewers approve the same manifest",
                ),
                command=(
                    "python",
                    "scripts/verify_first_stage.py",
                    "--submission",
                ),
                expected_result="submission verification valid=true",
            ),
        ]

    def build(
        self,
        entries: Iterable[RunbookEntry | dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        values = list(entries) if entries is not None else self.default_entries()
        rows: list[RunbookEntry] = []
        for index, raw in enumerate(values):
            rows.append(
                raw
                if isinstance(raw, RunbookEntry)
                else RunbookEntry.from_dict(raw, f"entries[{index}]")
            )
        ledger = FindingLedger()
        seen: set[str] = set()
        kinds: set[str] = set()
        requirement_ids: set[str] = set()
        output: list[dict[str, Any]] = []
        for entry in rows:
            if entry.entry_id in seen:
                ledger.blocker(
                    "duplicate-runbook-entry",
                    "runbook entry ids must be unique",
                    category="navigation",
                    entry_id=entry.entry_id,
                )
            seen.add(entry.entry_id)
            kinds.add(entry.kind)
            requirement_ids.update(entry.requirement_ids)
            path = (self.repository_root / entry.path).resolve()
            try:
                path.relative_to(self.repository_root)
            except ValueError:
                ledger.blocker(
                    "runbook-path-escape",
                    "runbook path resolves outside the repository",
                    category="navigation",
                    entry_id=entry.entry_id,
                    path=entry.path,
                )
            if entry.required and not path.exists():
                ledger.blocker(
                    "runbook-path-missing",
                    "required runbook target does not exist",
                    category="navigation",
                    entry_id=entry.entry_id,
                    path=entry.path,
                )
            if entry.command and any(
                token in " ".join(entry.command)
                for token in ("../", "..\\")
            ):
                ledger.blocker(
                    "runbook-root-dependency",
                    "runbook command contains a parent-directory reference",
                    category="navigation",
                    entry_id=entry.entry_id,
                )
            output.append(entry.to_dict())
        missing_kinds = sorted(self.REQUIRED_KINDS - kinds)
        if missing_kinds:
            ledger.blocker(
                "runbook-kinds-missing",
                "acceptance runbook lacks required navigation categories",
                category="navigation",
                missing=missing_kinds,
            )
        self._verify_evidence_index(requirement_ids, ledger)
        document = {
            "schema": "zyra.final-freeze.evidence-navigation.v1",
            "valid": ledger.valid,
            "entry_count": len(output),
            "covered_kinds": sorted(kinds),
            "covered_requirement_ids": sorted(requirement_ids),
            "entries": sorted(output, key=lambda item: item["entry_id"]),
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)

    def _verify_evidence_index(
        self,
        linked_requirements: set[str],
        ledger: FindingLedger,
    ) -> None:
        path = (
            self.freeze_output
            / "generated"
            / "100-point-evidence-index.json"
        )
        if not path.is_file():
            ledger.blocker(
                "evidence-index-missing",
                "freeze output has no evidence index",
                category="navigation",
                path=str(path),
            )
            return
        document = load_json(path, label="evidence_index")
        rows = document.get("requirements")
        admitted: set[str] = set()
        if isinstance(rows, dict):
            admitted.update(str(key) for key in rows)
        else:
            if not isinstance(rows, list):
                rows = document.get("rows", [])
            for index, raw in enumerate(
                require_sequence(rows, "evidence_index.requirements")
            ):
                row = require_mapping(
                    raw,
                    f"evidence_index.requirements[{index}]",
                )
                identity = row.get("requirement_id", row.get("id"))
                if isinstance(identity, str):
                    admitted.add(identity)
        missing = sorted(linked_requirements - admitted)
        if missing:
            ledger.blocker(
                "runbook-requirements-not-indexed",
                "runbook links requirement ids absent from evidence index",
                category="navigation",
                missing=missing,
            )


class EvidenceNavigationVerifier:
    """Independently validate a generated navigation document."""

    def verify(
        self,
        document: Any,
        *,
        repository_root: str | Path,
    ) -> dict[str, Any]:
        item = require_mapping(document, "navigation")
        ledger = FindingLedger()
        root = Path(repository_root).resolve()
        actual_digest = item.get("digest")
        expected_digest = digest(
            {key: value for key, value in item.items() if key != "digest"}
        )
        if actual_digest != expected_digest:
            ledger.blocker(
                "navigation-digest-mismatch",
                "navigation content does not match its embedded digest",
                category="navigation-verification",
                expected=expected_digest,
                actual=actual_digest,
            )
        entries = require_sequence(
            item.get("entries"),
            "navigation.entries",
        )
        kinds: set[str] = set()
        identifiers: set[str] = set()
        for index, raw in enumerate(entries):
            entry = RunbookEntry.from_dict(
                raw,
                f"navigation.entries[{index}]",
            )
            if entry.entry_id in identifiers:
                ledger.blocker(
                    "navigation-entry-duplicate",
                    "navigation contains duplicate entry id",
                    category="navigation-verification",
                    entry_id=entry.entry_id,
                )
            identifiers.add(entry.entry_id)
            kinds.add(entry.kind)
            path = (root / entry.path).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                ledger.blocker(
                    "navigation-path-escape",
                    "navigation entry resolves outside repository",
                    category="navigation-verification",
                    entry_id=entry.entry_id,
                )
            if entry.required and not path.exists():
                ledger.blocker(
                    "navigation-target-missing",
                    "required navigation target is missing",
                    category="navigation-verification",
                    entry_id=entry.entry_id,
                    path=entry.path,
                )
        missing = sorted(EvidenceNavigationBuilder.REQUIRED_KINDS - kinds)
        if missing:
            ledger.blocker(
                "navigation-coverage-incomplete",
                "navigation document omits required evidence categories",
                category="navigation-verification",
                missing=missing,
            )
        if item.get("valid") is not True:
            ledger.blocker(
                "navigation-producer-invalid",
                "navigation producer did not mark document valid",
                category="navigation-verification",
            )
        receipt = {
            "schema": "zyra.final-freeze.evidence-navigation-verification.v1",
            "valid": ledger.valid,
            "entry_count": len(entries),
            "covered_kinds": sorted(kinds),
            "navigation_digest": actual_digest,
            "findings": ledger.to_dict(),
        }
        return object_with_digest(receipt)
