"""Residual-work classification and fail-closed first-stage handoff."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .common import (
    FindingLedger,
    object_with_digest,
    require_boolean,
    require_choice,
    require_identity,
    require_integer,
    require_mapping,
    require_sequence,
    require_text,
    safe_relative_path,
    stable_unique,
)


HANDOFF_CLASSES = (
    "first-stage-blocker",
    "ci-hardening",
    "future-optimization",
)
HANDOFF_STATES = (
    "open",
    "accepted",
    "scheduled",
    "closed",
    "rejected",
)


@dataclass(frozen=True, slots=True)
class ResidualItem:
    """One identified residual before or after first-stage freeze."""

    item_id: str
    title: str
    description: str
    source: str
    severity: str
    impacts: tuple[str, ...]
    runtime_reachable: bool
    affects_submission: bool
    security_relevant: bool
    data_loss_possible: bool
    workaround: str
    evidence_paths: tuple[str, ...] = ()
    suggested_owner: str = ""

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "ResidualItem":
        item = require_mapping(value, label)
        return cls(
            item_id=require_identity(
                item.get("item_id"),
                f"{label}.item_id",
            ),
            title=require_text(item.get("title"), f"{label}.title"),
            description=require_text(
                item.get("description"),
                f"{label}.description",
                maximum=8192,
            ),
            source=require_identity(item.get("source"), f"{label}.source"),
            severity=require_choice(
                item.get("severity"),
                f"{label}.severity",
                ("observation", "warning", "blocker"),
            ),
            impacts=tuple(
                require_identity(entry, f"{label}.impacts[{index}]")
                for index, entry in enumerate(
                    require_sequence(
                        item.get("impacts"),
                        f"{label}.impacts",
                        minimum=1,
                    )
                )
            ),
            runtime_reachable=require_boolean(
                item.get("runtime_reachable"),
                f"{label}.runtime_reachable",
            ),
            affects_submission=require_boolean(
                item.get("affects_submission"),
                f"{label}.affects_submission",
            ),
            security_relevant=require_boolean(
                item.get("security_relevant"),
                f"{label}.security_relevant",
            ),
            data_loss_possible=require_boolean(
                item.get("data_loss_possible"),
                f"{label}.data_loss_possible",
            ),
            workaround=require_text(
                item.get("workaround", ""),
                f"{label}.workaround",
                allow_empty=True,
                maximum=4096,
            ),
            evidence_paths=tuple(
                safe_relative_path(entry, f"{label}.evidence_paths[{index}]")
                for index, entry in enumerate(
                    require_sequence(
                        item.get("evidence_paths", []),
                        f"{label}.evidence_paths",
                        allow_empty=True,
                    )
                )
            ),
            suggested_owner=require_identity(
                item.get("suggested_owner", "release-owner"),
                f"{label}.suggested_owner",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "severity": self.severity,
            "impacts": list(self.impacts),
            "runtime_reachable": self.runtime_reachable,
            "affects_submission": self.affects_submission,
            "security_relevant": self.security_relevant,
            "data_loss_possible": self.data_loss_possible,
            "workaround": self.workaround,
            "evidence_paths": list(self.evidence_paths),
            "suggested_owner": self.suggested_owner,
        }


@dataclass(frozen=True, slots=True)
class HandoffDecision:
    """Classified residual with explicit owner and completion definition."""

    item_id: str
    classification: str
    rationale: tuple[str, ...]
    owner: str
    target_stage: str
    state: str
    acceptance: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    risk_if_deferred: str
    rank: int

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "HandoffDecision":
        item = require_mapping(value, label)
        return cls(
            item_id=require_identity(
                item.get("item_id"),
                f"{label}.item_id",
            ),
            classification=require_choice(
                item.get("classification"),
                f"{label}.classification",
                HANDOFF_CLASSES,
            ),
            rationale=tuple(
                require_text(
                    entry,
                    f"{label}.rationale[{index}]",
                    maximum=4096,
                )
                for index, entry in enumerate(
                    require_sequence(
                        item.get("rationale"),
                        f"{label}.rationale",
                        minimum=1,
                    )
                )
            ),
            owner=require_identity(item.get("owner"), f"{label}.owner"),
            target_stage=require_choice(
                item.get("target_stage"),
                f"{label}.target_stage",
                ("first-stage", "ci", "post-submission"),
            ),
            state=require_choice(
                item.get("state"),
                f"{label}.state",
                HANDOFF_STATES,
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
            evidence_paths=tuple(
                safe_relative_path(entry, f"{label}.evidence_paths[{index}]")
                for index, entry in enumerate(
                    require_sequence(
                        item.get("evidence_paths", []),
                        f"{label}.evidence_paths",
                        allow_empty=True,
                    )
                )
            ),
            risk_if_deferred=require_text(
                item.get("risk_if_deferred"),
                f"{label}.risk_if_deferred",
                maximum=4096,
            ),
            rank=require_integer(
                item.get("rank"),
                f"{label}.rank",
                minimum=1,
                maximum=9999,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "classification": self.classification,
            "rationale": list(self.rationale),
            "owner": self.owner,
            "target_stage": self.target_stage,
            "state": self.state,
            "acceptance": list(self.acceptance),
            "evidence_paths": list(self.evidence_paths),
            "risk_if_deferred": self.risk_if_deferred,
            "rank": self.rank,
        }


class ResidualClassifier:
    """Deterministically classify residuals without LLM authority."""

    FIRST_STAGE_IMPACTS = {
        "default-runtime",
        "permission",
        "state-custody",
        "recovery",
        "submission",
        "clean-install",
        "score",
        "live-case",
        "artifact-integrity",
    }
    CI_IMPACTS = {
        "lint",
        "flaky-test",
        "dependency-audit",
        "mutation-coverage",
        "performance-regression",
        "long-duration-soak",
        "tooling",
    }

    def classify(
        self,
        residual: ResidualItem | dict[str, Any],
        *,
        rank: int,
    ) -> HandoffDecision:
        item = (
            residual
            if isinstance(residual, ResidualItem)
            else ResidualItem.from_dict(residual, "residual")
        )
        reasons: list[str] = []
        first_stage = False
        impacts = set(item.impacts)
        if item.severity == "blocker":
            first_stage = True
            reasons.append("source severity is blocker")
        if item.security_relevant:
            first_stage = True
            reasons.append("security boundary is affected")
        if item.data_loss_possible:
            first_stage = True
            reasons.append("data loss is possible")
        if item.affects_submission:
            first_stage = True
            reasons.append("final submission content or integrity is affected")
        if impacts & self.FIRST_STAGE_IMPACTS:
            first_stage = True
            reasons.append(
                "impact intersects a first-stage acceptance boundary"
            )
        if item.runtime_reachable and not item.workaround:
            first_stage = True
            reasons.append(
                "runtime-reachable residual has no bounded workaround"
            )
        if first_stage:
            classification = "first-stage-blocker"
            target_stage = "first-stage"
            state = "open"
            risk = (
                "Deferral would invalidate the first-stage freeze or "
                "submission."
            )
            acceptance = (
                "implement and test the corrective change",
                "re-run affected M3-03 incremental review checks",
                "rebuild final-freeze and submission receipts",
            )
        elif impacts and impacts.issubset(self.CI_IMPACTS):
            classification = "ci-hardening"
            target_stage = "ci"
            state = "accepted"
            reasons.append(
                "impact is limited to continuous verification hardening"
            )
            risk = (
                "Deferral reduces automated detection depth but does not "
                "change frozen product behavior."
            )
            acceptance = (
                "add a deterministic CI gate",
                "record failure and success fixtures",
                "assign a regression owner and threshold",
            )
        else:
            classification = "future-optimization"
            target_stage = "post-submission"
            state = "accepted"
            reasons.append(
                "residual does not affect frozen correctness or submission"
            )
            risk = (
                "Deferral preserves current behavior and postpones an "
                "optional improvement."
            )
            acceptance = (
                "define a measurable improvement target",
                "retain compatibility with the frozen first-stage contract",
            )
        return HandoffDecision(
            item_id=item.item_id,
            classification=classification,
            rationale=tuple(stable_unique(reasons)),
            owner=item.suggested_owner,
            target_stage=target_stage,
            state=state,
            acceptance=acceptance,
            evidence_paths=item.evidence_paths,
            risk_if_deferred=risk,
            rank=rank,
        )


class HandoffLedger:
    """Build and verify the authoritative post-freeze work ledger."""

    def __init__(self, classifier: ResidualClassifier | None = None) -> None:
        self.classifier = classifier or ResidualClassifier()

    def build(
        self,
        residuals: Iterable[ResidualItem | dict[str, Any]],
    ) -> dict[str, Any]:
        items: list[ResidualItem] = []
        for index, raw in enumerate(residuals):
            items.append(
                raw
                if isinstance(raw, ResidualItem)
                else ResidualItem.from_dict(raw, f"residuals[{index}]")
            )
        ledger = FindingLedger()
        seen: set[str] = set()
        decisions: list[HandoffDecision] = []
        sorted_items = sorted(
            items,
            key=lambda item: (
                _severity_rank(item.severity),
                not item.affects_submission,
                not item.runtime_reachable,
                item.item_id,
            ),
        )
        for rank, item in enumerate(sorted_items, start=1):
            if item.item_id in seen:
                ledger.blocker(
                    "duplicate-residual-id",
                    "residual work ledger contains a duplicate id",
                    category="handoff",
                    item_id=item.item_id,
                )
            seen.add(item.item_id)
            decision = self.classifier.classify(item, rank=rank)
            decisions.append(decision)
            if not decision.owner:
                ledger.blocker(
                    "handoff-owner-missing",
                    "classified residual has no owner",
                    category="handoff",
                    item_id=item.item_id,
                )
        blocker_count = sum(
            decision.classification == "first-stage-blocker"
            and decision.state != "closed"
            for decision in decisions
        )
        if blocker_count:
            ledger.blocker(
                "unresolved-first-stage-residuals",
                "first-stage blockers cannot be handed off or deferred",
                category="handoff",
                count=blocker_count,
                item_ids=[
                    decision.item_id
                    for decision in decisions
                    if decision.classification == "first-stage-blocker"
                    and decision.state != "closed"
                ],
            )
        counts = {
            classification: sum(
                decision.classification == classification
                for decision in decisions
            )
            for classification in HANDOFF_CLASSES
        }
        document = {
            "schema": "zyra.final-freeze.handoff-ledger.v1",
            "valid": ledger.valid,
            "residual_count": len(items),
            "classification_counts": counts,
            "decisions": [
                decision.to_dict()
                for decision in sorted(decisions, key=lambda item: item.rank)
            ],
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)

    def verify(self, document: Any) -> dict[str, Any]:
        item = require_mapping(document, "handoff_ledger")
        ledger = FindingLedger()
        decisions = require_sequence(
            item.get("decisions"),
            "handoff_ledger.decisions",
        )
        identifiers: set[str] = set()
        ranks: set[int] = set()
        counts = {key: 0 for key in HANDOFF_CLASSES}
        for index, raw in enumerate(decisions):
            decision = HandoffDecision.from_dict(
                raw,
                f"handoff_ledger.decisions[{index}]",
            )
            if decision.item_id in identifiers:
                ledger.blocker(
                    "duplicate-handoff-decision",
                    "handoff ledger contains a duplicate item",
                    category="handoff-verification",
                    item_id=decision.item_id,
                )
            if decision.rank in ranks:
                ledger.blocker(
                    "duplicate-handoff-rank",
                    "handoff ledger contains a duplicate rank",
                    category="handoff-verification",
                    rank=decision.rank,
                )
            identifiers.add(decision.item_id)
            ranks.add(decision.rank)
            counts[decision.classification] += 1
            self._verify_decision(decision, ledger)
        expected_ranks = set(range(1, len(decisions) + 1))
        if ranks != expected_ranks:
            ledger.blocker(
                "handoff-rank-gap",
                "handoff ranks must form a complete sequence",
                category="handoff-verification",
                expected=sorted(expected_ranks),
                actual=sorted(ranks),
            )
        if item.get("classification_counts") != counts:
            ledger.blocker(
                "handoff-count-mismatch",
                "handoff classification counts do not match decisions",
                category="handoff-verification",
                expected=counts,
                actual=item.get("classification_counts"),
            )
        if item.get("valid") is not True:
            ledger.blocker(
                "handoff-producer-invalid",
                "handoff ledger contains unresolved producer findings",
                category="handoff-verification",
            )
        receipt = {
            "schema": "zyra.final-freeze.handoff-verification.v1",
            "valid": ledger.valid,
            "residual_count": len(decisions),
            "classification_counts": counts,
            "findings": ledger.to_dict(),
        }
        return object_with_digest(receipt)

    def _verify_decision(
        self,
        decision: HandoffDecision,
        ledger: FindingLedger,
    ) -> None:
        expected_stage = {
            "first-stage-blocker": "first-stage",
            "ci-hardening": "ci",
            "future-optimization": "post-submission",
        }[decision.classification]
        if decision.target_stage != expected_stage:
            ledger.blocker(
                "handoff-stage-mismatch",
                "handoff target stage contradicts classification",
                category="handoff-verification",
                item_id=decision.item_id,
                expected=expected_stage,
                actual=decision.target_stage,
            )
        if (
            decision.classification == "first-stage-blocker"
            and decision.state != "closed"
        ):
            ledger.blocker(
                "first-stage-blocker-deferred",
                "an unresolved first-stage blocker cannot be handed off",
                category="handoff-verification",
                item_id=decision.item_id,
                state=decision.state,
            )
        if (
            decision.classification != "first-stage-blocker"
            and decision.state == "open"
        ):
            ledger.warning(
                "handoff-item-not-accepted",
                "non-blocking handoff item remains open",
                category="handoff-verification",
                item_id=decision.item_id,
            )


def empty_handoff_ledger() -> dict[str, Any]:
    """Return the valid explicit ledger used when no residuals remain."""

    return HandoffLedger().build([])


def _severity_rank(value: str) -> int:
    return {"blocker": 0, "warning": 1, "observation": 2}[value]
